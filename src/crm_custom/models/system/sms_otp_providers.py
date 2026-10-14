import logging
import os

import requests
from odoo import api, models

from ....util.phone import is_valid_th_msisdn, normalize_th_msisdn

_logger = logging.getLogger(__name__)

GODYSMS_PROVIDER = "godysms"
THAIBULKSMS_PROVIDER = "thaibulksms"

THAIBULKSMS_OTP_CREDIT_CACHE_KEY = "crm.otp.thaibulksms_credit_remain"


def _env(name, default=False):
    return os.getenv(name, default) or default


def _parse_remaining_credit(payload):
    if not isinstance(payload, dict):
        return 0

    remaining = payload.get("remaining_credit", payload.get("balance"))
    if isinstance(remaining, dict):
        return int(remaining.get("standard") or 0) + int(remaining.get("corporate") or 0)
    if isinstance(remaining, (int, float)):
        return int(remaining)

    credit_remain = payload.get("credit_remain")
    if isinstance(credit_remain, (int, float)):
        return int(credit_remain)

    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("credit_remain")
        if isinstance(nested, (int, float)):
            return int(nested)

    return 0


def _parse_standard_sms_credit(payload):
    if not isinstance(payload, dict):
        return 0

    remaining = payload.get("remaining_credit", payload.get("balance"))
    if isinstance(remaining, dict):
        return int(remaining.get("standard") or 0)
    if isinstance(remaining, (int, float)):
        return int(remaining)
    return 0


class SmsOtpProviderMixin(models.AbstractModel):
    _name = "crm.sms.otp.provider.mixin"
    _description = "SMS OTP provider helpers"

    def _ensure_sms_msisdn(self):
        self.ensure_one()
        msisdn = normalize_th_msisdn(self.recipient)
        if msisdn and msisdn != self.recipient:
            self.recipient = msisdn
        if not is_valid_th_msisdn(msisdn):
            return None
        return msisdn

    @api.model
    def _godysms_configured(self):
        return all(
            [
                _env("SMS_API_ENDPOINT"),
                _env("SMS_API_TOKEN"),
                _env("SMS_APP_KEY"),
                _env("SMS_APP_SECRET"),
            ]
        )

    @api.model
    def _thaibulksms_otp_configured(self):
        return all([_env("THAIBULKSMS_OTP_KEY"), _env("THAIBULKSMS_OTP_SECRET")])

    @api.model
    def _thaibulksms_otp_force(self):
        """OTP API defaults to corporate credit; use standard/corporate via force param."""
        force = (_env("THAIBULKSMS_OTP_FORCE", "standard") or "standard").strip().lower()
        if force in ("standard", "corporate"):
            return force
        if force in ("premium",):
            return "corporate"
        return False

    @api.model
    def _thaibulksms_otp_payload_extras(self):
        extras = {}
        force = self._thaibulksms_otp_force()
        if force:
            extras["force"] = force
        return extras

    @api.model
    def _thaibulksms_sms_api_configured(self):
        return all([_env("THAIBULKSMS_API_KEY"), _env("THAIBULKSMS_API_SECRET")])

    @api.model
    def _godysms_remaining_credit(self):
        if not self._godysms_configured():
            return 0

        base_url = _env("SMS_API_ENDPOINT").rstrip("/")
        url = f"{base_url}/openapi/sms/balance"
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {_env('SMS_API_TOKEN')}",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            _logger.warning("crm.otp godysms credit check failed: %s", exc)
            return 0

        if response.status_code != 200:
            return 0

        return _parse_remaining_credit(response.json())

    @api.model
    def _thaibulksms_sms_remaining_credit(self):
        if not self._thaibulksms_sms_api_configured():
            return None

        url = "https://api-v2.thaibulksms.com/credit"
        try:
            response = requests.get(
                url,
                auth=(_env("THAIBULKSMS_API_KEY"), _env("THAIBULKSMS_API_SECRET")),
                timeout=30,
            )
        except requests.RequestException as exc:
            _logger.warning("crm.otp thaibulksms SMS credit check failed: %s", exc)
            return 0

        if response.status_code != 200:
            return 0

        return _parse_remaining_credit(response.json())

    @api.model
    def _thaibulksms_standard_sms_remaining_credit(self):
        if not self._thaibulksms_sms_api_configured():
            return None

        url = "https://api-v2.thaibulksms.com/credit"
        try:
            response = requests.get(
                url,
                auth=(_env("THAIBULKSMS_API_KEY"), _env("THAIBULKSMS_API_SECRET")),
                timeout=30,
            )
        except requests.RequestException as exc:
            _logger.warning("crm.otp thaibulksms standard credit check failed: %s", exc)
            return 0

        if response.status_code != 200:
            return 0

        return _parse_standard_sms_credit(response.json())

    @api.model
    def _thaibulksms_cached_otp_credit(self):
        icp = self.env["ir.config_parameter"].sudo()
        raw = icp.get_param(THAIBULKSMS_OTP_CREDIT_CACHE_KEY)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @api.model
    def _set_thaibulksms_cached_otp_credit(self, credit_remain):
        if credit_remain is None:
            return
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(THAIBULKSMS_OTP_CREDIT_CACHE_KEY, str(int(credit_remain)))

    @api.model
    def _thaibulksms_remaining_credit(self):
        if not self._thaibulksms_otp_configured():
            return 0

        if self._thaibulksms_otp_force() == "standard":
            standard_credit = self._thaibulksms_standard_sms_remaining_credit()
            if standard_credit is not None:
                return standard_credit

        cached = self._thaibulksms_cached_otp_credit()
        if cached is not None:
            return cached

        return 0

    @api.model
    def _provider_has_credit(self, provider):
        if provider == GODYSMS_PROVIDER:
            return self._godysms_remaining_credit() >= 1
        if provider == THAIBULKSMS_PROVIDER:
            return self._thaibulksms_remaining_credit() >= 1
        return False

    @api.model
    def _pick_weighted_provider(self):
        param = self.env["crm.system.parameter"]
        godysms_weight = param.get_int("sms.godysms_rate", 50)
        godysms_weight = max(0, min(100, godysms_weight))
        thaibulksms_weight = 100 - godysms_weight

        weighted = []
        if self._provider_has_credit(GODYSMS_PROVIDER) and godysms_weight > 0:
            weighted.append((GODYSMS_PROVIDER, godysms_weight))
        if self._provider_has_credit(THAIBULKSMS_PROVIDER) and thaibulksms_weight > 0:
            weighted.append((THAIBULKSMS_PROVIDER, thaibulksms_weight))

        if not weighted:
            for provider in (GODYSMS_PROVIDER, THAIBULKSMS_PROVIDER):
                if self._provider_has_credit(provider):
                    return provider
            return None

        if len(weighted) == 1:
            return weighted[0][0]

        import secrets

        total = sum(weight for _, weight in weighted)
        pick = secrets.randbelow(total)
        acc = 0
        for provider, weight in weighted:
            acc += weight
            if pick < acc:
                return provider
        return weighted[-1][0]

    def _send_godysms_otp(self):
        msisdn = self._ensure_sms_msisdn()
        if not msisdn:
            return None, "Invalid Thai mobile number (use 0XXXXXXXXX or 66XXXXXXXXX)."

        base_url = _env("SMS_API_ENDPOINT").rstrip("/")
        url = f"{base_url}/openapi/sms/app/otp"
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {_env('SMS_API_TOKEN')}",
            "secret": f"{_env('SMS_APP_SECRET')}",
            "content-type": "application/json",
        }
        payload = {
            "msisdn": msisdn,
            "appKey": _env("SMS_APP_KEY"),
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            return None, f"failed to send sms otp (godysms): {exc}"

        if response.status_code not in (200, 201):
            return None, f"failed to send sms otp (godysms): {response.text}"

        response_data = response.json()
        return {
            "ref": response_data.get("ref", False),
            "transaction_id": response_data.get("transactionId")
            or response_data.get("transaction_id", False),
        }, response.text

    def _send_thaibulksms_otp(self):
        msisdn = self._ensure_sms_msisdn()
        if not msisdn:
            return None, "Invalid Thai mobile number (use 0XXXXXXXXX or 66XXXXXXXXX)."

        base_url = _env("THAIBULKSMS_OTP_ENDPOINT", "https://otp.thaibulksms.com").rstrip("/")
        url = f"{base_url}/v2/otp/request"
        payload = {
            "key": _env("THAIBULKSMS_OTP_KEY"),
            "secret": _env("THAIBULKSMS_OTP_SECRET"),
            "msisdn": msisdn,
            **self._thaibulksms_otp_payload_extras(),
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            return None, f"failed to send sms otp (thaibulksms): {exc}"

        if response.status_code not in (200, 201):
            if "INSUFFICIENT_CREDIT" in (response.text or "").upper():
                self._set_thaibulksms_cached_otp_credit(0)
            return None, f"failed to send sms otp (thaibulksms): {response.text}"

        response_data = response.json()
        credit_remain = _parse_remaining_credit(response_data)
        if credit_remain:
            self._set_thaibulksms_cached_otp_credit(credit_remain)

        data = response_data.get("data") if isinstance(response_data.get("data"), dict) else {}
        token = response_data.get("token") or data.get("token")
        ref = (
            response_data.get("ref")
            or response_data.get("ref_code")
            or data.get("ref_code")
            or data.get("ref")
        )

        if not token:
            return None, f"failed to send sms otp (thaibulksms): missing token in {response.text}"

        return {
            "ref": ref or False,
            "transaction_id": token,
        }, response.text

    def _verify_godysms_otp(self, otp_code):
        msisdn = self._ensure_sms_msisdn()
        if not msisdn:
            return False

        base_url = _env("SMS_API_ENDPOINT").rstrip("/")
        url = f"{base_url}/openapi/verify/otp"
        payload = {
            "msisdn": msisdn,
            "otp": otp_code,
            "transaction_id": self.otp,
        }
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {_env('SMS_API_TOKEN')}",
            "content-type": "application/json",
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
        except requests.RequestException:
            return False

        if response.status_code != 200:
            return False

        return response.json().get("status", False)

    def _verify_thaibulksms_otp(self, otp_code):
        base_url = _env("THAIBULKSMS_OTP_ENDPOINT", "https://otp.thaibulksms.com").rstrip("/")
        url = f"{base_url}/v2/otp/verify"
        payload = {
            "key": _env("THAIBULKSMS_OTP_KEY"),
            "secret": _env("THAIBULKSMS_OTP_SECRET"),
            "token": self.otp,
            "pin": otp_code,
            **self._thaibulksms_otp_payload_extras(),
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException:
            return False

        if response.status_code not in (200, 201):
            return False

        body = response.json()
        status = body.get("status")
        if status is True or status == "verified":
            return True

        data = body.get("data")
        if isinstance(data, dict):
            nested = data.get("status")
            if nested is True or nested == "verified":
                return True
            if data.get("verify") is True:
                return True

        return body.get("verify") is True
