import json
import os
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import requests

from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.crm_custom.controllers.user.get_user_info import GetOrCreateUserController
from odoo.addons.crm_custom.controllers.user.update_user import UpdateUserController
from odoo.addons.crm_custom.models.system.otp import SMS_OTP_CREDIT_CACHE_TTL_PARAM
from odoo.addons.crm_custom.models.system.sms_otp_providers import GODYSMS_PROVIDER
from odoo.addons.crm_custom.models.user.zortout_sync import CrmUserZortoutSync

LINE_NAME = "Line Member"
LINE_PICTURE = "https://example.com/line.png"
LINE_USER_ID = "Ulineprofile1"
MEMBER_TIER = {
    "code": "member",
    "name": "Member",
    "min_spending": 0.0,
    "max_spending": 999999999.0,
    "color": False,
    "image_url": False,
}
_ORIGINAL_SESSION_REQUEST = requests.sessions.Session.request
SMS_ENV = {
    "SMS_API_ENDPOINT": "http://sms.test",
    "SMS_API_TOKEN": "token",
    "SMS_APP_KEY": "key",
    "SMS_APP_SECRET": "secret",
}
SMS_ENV_CLEAR = (
    "THAIBULKSMS_OTP_KEY",
    "THAIBULKSMS_OTP_SECRET",
    "THAIBULKSMS_API_KEY",
    "THAIBULKSMS_API_SECRET",
    "THAIBULKSMS_OTP_FORCE",
)
RESPONSE_KEYS = {
    "display_name",
    "picture_url",
    "line_user_id",
    "email",
    "phone",
    "force_verify_phone",
    "force_verify_email",
    "birth_date",
    "gender",
    "address",
    "is_updated_user_info",
    "tier",
}


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.reason = "OK" if self.ok else "Error"
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


def _external_route(outbound):
    """Mock LINE, SMS, and Zortout. Leave the test HTTP client alone."""

    def route(session, method, url, **kwargs):
        host = urlparse(url).hostname or ""
        path = urlparse(url).path
        if host in {"api.line.me", "open-api.zortout.com", "sms.test", "api-v2.thaibulksms.com"} or path.endswith(
            "/openapi/sms/balance"
        ):
            return outbound(method, url, **kwargs)
        return _ORIGINAL_SESSION_REQUEST(session, method, url, **kwargs)

    return route


class _Outbound:
    def __init__(self, line_name=LINE_NAME):
        self.line_name = line_name
        self.line_picture = LINE_PICTURE
        self.user_id = LINE_USER_ID
        self.calls = []
        self.sms_credit = 25
        self.sms_error = None

    def __call__(self, method, url, **kwargs):
        method = method.upper()
        self.calls.append((method, url))
        path = urlparse(url).path
        if "api.line.me" in url:
            headers = kwargs.get("headers") or {}
            token = headers.get("Authorization") or headers.get("authorization") or ""
            name = "Renamed Member" if "renamed" in token else self.line_name
            return _FakeResponse(200, {
                "userId": self.user_id,
                "displayName": name,
                "pictureUrl": self.line_picture,
            })
        if path.endswith("/openapi/sms/balance"):
            if self.sms_error is not None:
                raise self.sms_error
            return _FakeResponse(200, {"remaining_credit": self.sms_credit})
        if "zortout.com" in url and method == "GET":
            return _FakeResponse(200, {"resCode": "200", "resDesc": "Success", "list": []})
        if "zortout.com" in url and method == "POST":
            return _FakeResponse(200, {
                "resCode": "200",
                "resDesc": "Success",
                "detail": {"id": 42},
            })
        raise AssertionError(f"unexpected outbound {method} {url}")

    def sms_urls(self):
        return [url for method, url in self.calls if urlparse(url).path.endswith("/openapi/sms/balance")]

    def zortout_calls(self):
        return [(method, url) for method, url in self.calls if "zortout.com" in url]


def _run_sync_callback(test, callback, expected_user_id):
    user_row = MagicMock()
    user_row.id = expected_user_id
    records = MagicMock()
    records.exists.return_value = [user_row]
    model = MagicMock()
    model.browse.return_value = records
    fake_env = MagicMock()
    fake_env.__getitem__.return_value = model
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = MagicMock()
    cursor_cm.__exit__.return_value = False
    with patch("odoo.modules.registry.Registry") as registry, patch(
        "odoo.api.Environment", return_value=fake_env
    ):
        registry.return_value.cursor.return_value = cursor_cm
        callback()
    registry.assert_called_once_with(test.env.cr.dbname)
    model.browse.assert_called_once_with([expected_user_id])
    user_row.partner_id.sync_member_to_zortout.assert_called_once_with(user_row)


@tagged("post_install", "-at_install")
class TestZortoutAutoSyncValueAware(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["partner"].create({
            "name": "Zortout Sync Tenant",
            "slug": "zortout-sync-tenant",
        })
        self.user = self.env["crm.user"].create({
            "display_name": "Member",
            "picture_url": "https://example.com/a.png",
            "line_user_id": "Uzortout1",
            "partner_id": self.partner.id,
        })
        self.partner.write({
            "zortout_enabled": True,
            "zortout_api_key": "key",
            "zortout_api_secret": "secret",
            "zortout_store_name": "store",
        })
        self.env.cr.postcommit.clear()

    def test_unchanged_sync_fields_do_not_schedule(self):
        self.user.write({
            "display_name": self.user.display_name,
            "picture_url": "https://example.com/other.png",
            "phone": "",
            "email": False,
        })
        self.assertEqual(len(self.env.cr.postcommit), 0)
        self.assertEqual(self.user.picture_url, "https://example.com/other.png")

    def test_changed_display_name_schedules_same_callback(self):
        self.user.write({"display_name": "Member Renamed"})
        self.assertEqual(self.user.display_name, "Member Renamed")
        self.assertEqual(len(self.env.cr.postcommit), 1)
        _run_sync_callback(self, self.env.cr.postcommit._funcs[0], self.user.id)

    def test_real_phone_change_still_schedules_when_name_is_unchanged(self):
        self.user.write({
            "display_name": "Member",
            "phone": "0812345678",
        })
        self.assertEqual(len(self.env.cr.postcommit), 1)

    def test_only_changed_records_are_scheduled(self):
        other = self.env["crm.user"].create({
            "display_name": "Other",
            "picture_url": "https://example.com/b.png",
            "line_user_id": "Uzortout2",
            "partner_id": self.partner.id,
        })
        self.env.cr.postcommit.clear()
        (self.user | other).write({"display_name": self.user.display_name})
        self.assertEqual(len(self.env.cr.postcommit), 1)
        _run_sync_callback(self, self.env.cr.postcommit._funcs[0], other.id)

    def test_create_still_schedules(self):
        self.env.cr.postcommit.clear()
        created = self.env["crm.user"].create({
            "display_name": "Created",
            "picture_url": "https://example.com/c.png",
            "line_user_id": "Uzortout3",
            "partner_id": self.partner.id,
        })
        self.assertEqual(len(self.env.cr.postcommit), 1)
        _run_sync_callback(self, self.env.cr.postcommit._funcs[0], created.id)

    def test_skip_context_suppresses_sync(self):
        self.user.with_context(skip_zortout_auto_sync=True).write({
            "display_name": "Skipped",
        })
        self.assertEqual(self.user.display_name, "Skipped")
        self.assertEqual(len(self.env.cr.postcommit), 0)

    def test_blank_phone_matches_empty_stored_value(self):
        self.assertFalse(self.user.phone)
        self.assertTrue(self.user._field_value_matches("phone", ""))
        self.assertTrue(self.user._field_value_matches("phone", False))
        self.assertFalse(self.user._field_value_matches("phone", "0899999999"))


@tagged("post_install", "-at_install")
class TestSmsOtpCreditCache(TransactionCase):
    def setUp(self):
        super().setUp()
        self.otp = self.env["crm.otp"]
        self.otp._clear_sms_otp_credit_cache()
        self.addCleanup(self.otp._clear_sms_otp_credit_cache)
        self.clock = {"now": 0.0}
        self.clock_patch = patch(
            "odoo.addons.crm_custom.models.system.otp._sms_credit_now",
            side_effect=lambda: self.clock["now"],
        )
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def _set_ttl(self, value):
        self.env["ir.config_parameter"].sudo().set_param(
            SMS_OTP_CREDIT_CACHE_TTL_PARAM,
            value,
        )

    def _patch_status(self, godysms, thaibulk):
        return (
            patch.object(type(self.otp), "_godysms_credit_status", side_effect=godysms),
            patch.object(type(self.otp), "_thaibulksms_credit_status", side_effect=thaibulk),
        )

    def test_hit_miss_and_ttl_do_not_hide_out_of_credit(self):
        self._set_ttl("30")
        state = {"credit": 5}
        godysms_calls = []

        def godysms():
            godysms_calls.append(state["credit"])
            return state["credit"], True

        def thaibulk():
            return 0, True

        godysms_patch, thaibulk_patch = self._patch_status(godysms, thaibulk)
        with godysms_patch as gody, thaibulk_patch as thai:
            self.assertTrue(self.otp.has_sms_otp_credit())
            state["credit"] = 0
            self.clock["now"] = 10
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 1)
            self.assertEqual(thai.call_count, 0)
            self.clock["now"] = 31
            self.assertFalse(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)
            self.assertEqual(len(godysms_calls), 2)

    def test_ttl_comes_from_config_and_invalid_values_use_the_default(self):
        self._set_ttl("10")

        def godysms():
            return 3, True

        godysms_patch, thaibulk_patch = self._patch_status(godysms, lambda: (0, True))
        with godysms_patch as gody, thaibulk_patch:
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.clock["now"] = 9
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 1)
            self.clock["now"] = 10
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)

        self.otp._clear_sms_otp_credit_cache()
        self.clock["now"] = 0
        self._set_ttl("nope")
        with godysms_patch as gody, thaibulk_patch:
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.clock["now"] = 119
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 1)
            self.clock["now"] = 120
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)

    def test_missing_parameter_uses_default_ttl(self):
        param = self.env["ir.config_parameter"].sudo().search([
            ("key", "=", SMS_OTP_CREDIT_CACHE_TTL_PARAM),
        ])
        if param:
            param.unlink()

        def godysms():
            return 2, True

        godysms_patch, thaibulk_patch = self._patch_status(godysms, lambda: (0, True))
        with godysms_patch as gody, thaibulk_patch:
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.clock["now"] = 119
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 1)
            self.clock["now"] = 120
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)

    def test_zero_ttl_disables_cache(self):
        self._set_ttl("0")
        godysms_patch, thaibulk_patch = self._patch_status(lambda: (4, True), lambda: (0, True))
        with godysms_patch as gody, thaibulk_patch:
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)

    def test_provider_error_is_not_cached(self):
        self._set_ttl("120")
        calls = {"n": 0}

        def godysms():
            calls["n"] += 1
            if calls["n"] == 1:
                return 0, False
            return 6, True

        godysms_patch, thaibulk_patch = self._patch_status(godysms, lambda: (0, True))
        with godysms_patch, thaibulk_patch as thai:
            self.assertFalse(self.otp.has_sms_otp_credit())
            self.assertEqual(thai.call_count, 1)
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(calls["n"], 2)
            self.assertEqual(thai.call_count, 1)

    def test_confirmed_credit_is_cached_when_the_other_provider_errors(self):
        self._set_ttl("120")
        godysms_patch, thaibulk_patch = self._patch_status(
            lambda: (0, False),
            lambda: (8, True),
        )
        with godysms_patch as gody, thaibulk_patch as thai:
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 1)
            self.assertEqual(thai.call_count, 1)

    def test_exception_is_not_cached_as_credit(self):
        self._set_ttl("120")

        def godysms():
            raise RuntimeError("provider blew up")

        godysms_patch, thaibulk_patch = self._patch_status(godysms, lambda: (9, True))
        with godysms_patch as gody, thaibulk_patch as thai:
            with self.assertRaises(RuntimeError):
                self.otp.has_sms_otp_credit()
            self.assertEqual(thai.call_count, 0)
            gody.side_effect = lambda: (4, True)
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 2)

    def test_http_error_is_not_cached_as_credit(self):
        self._set_ttl("120")
        with patch.dict(os.environ, SMS_ENV, clear=False):
            for key in SMS_ENV_CLEAR:
                os.environ.pop(key, None)
            with patch(
                "odoo.addons.crm_custom.models.system.sms_otp_providers.requests.get",
                side_effect=requests.RequestException("down"),
            ) as get:
                self.assertFalse(self.otp.has_sms_otp_credit())
                self.assertFalse(self.otp.has_sms_otp_credit())
                self.assertGreaterEqual(get.call_count, 2)

    def test_definitive_zero_balance_is_cached(self):
        self._set_ttl("120")
        with patch.dict(os.environ, SMS_ENV, clear=False):
            for key in SMS_ENV_CLEAR:
                os.environ.pop(key, None)

            def get(url, headers=None, timeout=None, auth=None):
                return _FakeResponse(200, {"remaining_credit": 0})

            with patch(
                "odoo.addons.crm_custom.models.system.sms_otp_providers.requests.get",
                side_effect=get,
            ) as mocked:
                self.assertFalse(self.otp.has_sms_otp_credit())
                self.assertFalse(self.otp.has_sms_otp_credit())
                self.assertEqual(mocked.call_count, 1)

    def test_send_path_does_not_use_the_cache(self):
        self._set_ttl("120")
        godysms_patch, thaibulk_patch = self._patch_status(lambda: (5, True), lambda: (0, True))
        with godysms_patch as gody, thaibulk_patch:
            self.assertTrue(self.otp._provider_has_credit(GODYSMS_PROVIDER))
            self.assertTrue(self.otp._provider_has_credit(GODYSMS_PROVIDER))
            self.assertEqual(gody.call_count, 2)
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertTrue(self.otp.has_sms_otp_credit())
            self.assertEqual(gody.call_count, 3)


@tagged("post_install", "-at_install")
class TestUserProfileRefresh(TransactionCase):
    def setUp(self):
        super().setUp()
        suffix = "refresh"
        self.partner = self.env["partner"].create({
            "name": "Profile Tenant",
            "slug": f"profile-tenant-{suffix}",
        })
        self.partner.write({"ui_crm_required_phone": True})
        self.user = self.env["crm.user"].create({
            "display_name": LINE_NAME,
            "picture_url": LINE_PICTURE,
            "line_user_id": LINE_USER_ID,
            "partner_id": self.partner.id,
        })
        self.partner.write({
            "zortout_enabled": True,
            "zortout_api_key": "key",
            "zortout_api_secret": "secret",
            "zortout_store_name": "store",
        })
        self.user._update_tier()
        self.env.cr.postcommit.clear()
        self.env["crm.otp"]._clear_sms_otp_credit_cache()
        self.addCleanup(self.env["crm.otp"]._clear_sms_otp_credit_cache)
        self.outbound = _Outbound()
        self.env_patch = patch.dict(os.environ, SMS_ENV, clear=False)
        self.env_patch.start()
        for key in SMS_ENV_CLEAR:
            os.environ.pop(key, None)
        self.addCleanup(self.env_patch.stop)

    def _request(self, token="Bearer line-token", body=b"{}"):
        headers = {}
        if token:
            headers["Authorization"] = token

        class _Headers(dict):
            def get(self, key, default=None):
                if str(key).lower() == "authorization":
                    for item_key, value in self.items():
                        if str(item_key).lower() == "authorization":
                            return value
                    return default
                return super().get(key, default)

        httprequest = MagicMock()
        httprequest.headers = _Headers(headers)
        httprequest.get_data.return_value = body.decode() if isinstance(body, bytes) else body
        return MagicMock(env=self.env, httprequest=httprequest)

    def _invoke(self, controller_call, token="Bearer line-token", body=b"{}"):
        fake_request = self._request(token=token, body=body)
        patches = [
            patch("requests.sessions.Session.request", _external_route(self.outbound)),
            patch("odoo.addons.crm_custom.controllers.user.get_user_info.request", fake_request),
            patch("odoo.addons.crm_custom.controllers.user.update_user.request", fake_request),
            patch("odoo.addons.util.portal_auth.request", fake_request),
        ]
        for item in patches:
            item.start()
        try:
            response = controller_call()
        finally:
            for item in reversed(patches):
                item.stop()
        payload = json.loads(response.get_data(as_text=True))
        return response.status_code, payload

    def _get(self, slug=None, token="Bearer line-token"):
        controller = GetOrCreateUserController()
        return self._invoke(
            lambda: controller.get_user_info(slug or self.partner.slug),
            token=token,
        )

    def _put(self, payload, token="Bearer line-token"):
        controller = UpdateUserController()
        return self._invoke(
            lambda: controller.update_user_info(self.partner.slug),
            token=token,
            body=json.dumps(payload),
        )

    def _expected_body(self, **overrides):
        body = {
            "display_name": LINE_NAME,
            "picture_url": LINE_PICTURE,
            "line_user_id": LINE_USER_ID,
            "email": False,
            "phone": False,
            "force_verify_phone": True,
            "force_verify_email": False,
            "birth_date": False,
            "gender": False,
            "address": False,
            "is_updated_user_info": False,
            "tier": dict(MEMBER_TIER),
        }
        body.update(overrides)
        return body

    def test_get_unchanged_profile_does_not_write_or_sync(self):
        write_date = self.user.write_date
        self.outbound.calls.clear()
        status, body = self._get()
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual(body, self._expected_body())
        self.assertEqual(self.user.write_date, write_date)
        self.assertEqual(self.user.display_name, LINE_NAME)
        self.assertEqual(len(self.env.cr.postcommit), 0)
        self.assertEqual(self.outbound.zortout_calls(), [])

    def test_get_twice_uses_the_credit_cache(self):
        self.outbound.calls.clear()
        first_status, first_body = self._get()
        second_status, second_body = self._get()
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first_body, second_body)
        self.assertEqual(len(self.outbound.sms_urls()), 1)
        self.assertEqual(len(self.env.cr.postcommit), 0)

    def test_get_changed_display_name_writes_and_syncs(self):
        self.outbound.line_name = "Renamed Member"
        status, body = self._get()
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "Renamed Member")
        self.assertEqual(body["picture_url"], LINE_PICTURE)
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual(self.user.display_name, "Renamed Member")
        self.assertEqual(len(self.env.cr.postcommit), 1)
        self.assertEqual(self.outbound.zortout_calls(), [])
        _run_sync_callback(self, self.env.cr.postcommit._funcs[0], self.user.id)

    def test_get_picture_change_does_not_sync(self):
        self.outbound.line_picture = "https://example.com/new.png"
        status, body = self._get()
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(body["picture_url"], "https://example.com/new.png")
        self.assertEqual(self.user.picture_url, "https://example.com/new.png")
        self.assertEqual(len(self.env.cr.postcommit), 0)

    def test_get_unknown_line_user_still_creates_and_schedules(self):
        self.outbound.calls.clear()
        created_id = "Ulineprofile-new"
        self.outbound.user_id = created_id
        status, body = self._get()
        created = self.env["crm.user"].search([
            ("line_user_id", "=", created_id),
            ("partner_id", "=", self.partner.id),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["line_user_id"], created_id)
        self.assertTrue(created)
        self.assertEqual(len(self.env.cr.postcommit), 1)

    def test_missing_partner_stays_404(self):
        status, body = self._get(slug="no-such-partner")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "partner_not_found")

    def test_missing_token_stays_401(self):
        status, body = self._get(token="")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_put_unchanged_profile_does_not_sync(self):
        self.user.write({"is_updated_user_info": True, "address": "Bangkok"})
        self.env.cr.postcommit.clear()
        write_date = self.user.write_date
        status, body = self._put({
            "display_name": LINE_NAME,
            "address": "Bangkok",
        })
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], LINE_NAME)
        self.assertEqual(body["address"], "Bangkok")
        self.assertTrue(body["is_updated_user_info"])
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual(self.user.write_date, write_date)
        self.assertEqual(len(self.env.cr.postcommit), 0)
        self.assertEqual(self.outbound.zortout_calls(), [])

    def test_put_changed_display_name_still_syncs(self):
        status, body = self._put({"display_name": "From Put"})
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "From Put")
        self.assertEqual(self.user.display_name, "From Put")
        self.assertTrue(body["is_updated_user_info"])
        self.assertEqual(len(self.env.cr.postcommit), 1)
        _run_sync_callback(self, self.env.cr.postcommit._funcs[0], self.user.id)

    def test_put_keeps_non_profile_changes(self):
        status, body = self._put({
            "display_name": LINE_NAME,
            "birth_date": "1991-05-06",
            "gender": "F",
        })
        self.user.invalidate_recordset()
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], LINE_NAME)
        self.assertEqual(body["birth_date"], "1991-05-06")
        self.assertEqual(body["gender"], "F")
        self.assertEqual(str(self.user.birth_date), "1991-05-06")
        self.assertEqual(len(self.env.cr.postcommit), 0)


@tagged("post_install", "-at_install")
class TestUserProfileHttp(HttpCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["partner"].create({
            "name": "HTTP Profile Tenant",
            "slug": "http-profile-tenant",
        })
        self.partner.write({"ui_crm_required_phone": True})
        self.user = self.env["crm.user"].create({
            "display_name": LINE_NAME,
            "picture_url": LINE_PICTURE,
            "line_user_id": LINE_USER_ID,
            "partner_id": self.partner.id,
        })
        self.partner.write({
            "zortout_enabled": True,
            "zortout_api_key": "key",
            "zortout_api_secret": "secret",
            "zortout_store_name": "store",
        })
        self.user._update_tier()
        self.env["crm.otp"]._clear_sms_otp_credit_cache()
        self.addCleanup(self.env["crm.otp"]._clear_sms_otp_credit_cache)
        self.outbound = _Outbound()
        self.scheduled = []
        self.env_patch = patch.dict(os.environ, SMS_ENV, clear=False)
        self.env_patch.start()
        for key in SMS_ENV_CLEAR:
            os.environ.pop(key, None)
        self.addCleanup(self.env_patch.stop)
        real_schedule = CrmUserZortoutSync._schedule_zortout_auto_sync

        def _record_schedule(recordset):
            self.scheduled.append(list(recordset.ids))
            return real_schedule(recordset)

        self.schedule_patch = patch.object(
            CrmUserZortoutSync,
            "_schedule_zortout_auto_sync",
            _record_schedule,
        )
        self.schedule_patch.start()
        self.addCleanup(self.schedule_patch.stop)
        self.http_patch = patch("requests.sessions.Session.request", _external_route(self.outbound))
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    def _write_date(self):
        self.env.cr.execute(
            "SELECT write_date FROM crm_user WHERE id = %s",
            [self.user.id],
        )
        return self.env.cr.fetchone()[0]

    def test_http_get_unchanged_profile_does_not_write_or_sync(self):
        write_date = self._write_date()
        self.outbound.calls.clear()
        self.scheduled.clear()
        first = self.url_open(
            f"/api/partner/{self.partner.slug}/user",
            headers={"Authorization": "Bearer line-token"},
        )
        second = self.url_open(
            f"/api/partner/{self.partner.slug}/user",
            headers={"Authorization": "Bearer line-token"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        body = first.json()
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual(body["display_name"], LINE_NAME)
        self.assertEqual(body["picture_url"], LINE_PICTURE)
        self.assertTrue(body["force_verify_phone"])
        self.assertEqual(body, second.json())
        self.assertEqual(self._write_date(), write_date)
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.outbound.zortout_calls(), [])
        self.assertEqual(len(self.outbound.sms_urls()), 1)

    def test_http_get_changed_display_name_still_schedules_sync(self):
        self.scheduled.clear()
        response = self.url_open(
            f"/api/partner/{self.partner.slug}/user",
            headers={"Authorization": "Bearer line-token-renamed"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual(body["display_name"], "Renamed Member")
        self.assertEqual(self.scheduled, [[self.user.id]])
        self.env.cr.execute(
            "SELECT display_name FROM crm_user WHERE id = %s",
            [self.user.id],
        )
        self.assertEqual(self.env.cr.fetchone()[0], "Renamed Member")
