import logging
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ....util.lazada_signature import build_lazada_signature

_logger = logging.getLogger(__name__)

LAZADA_API_BASE_URL = "https://api.lazada.co.th/rest"
LAZADA_AUTH_BASE_URL = "https://auth.lazada.com/rest"
LAZADA_QUALIFYING_STATUSES = ["delivered"]
LAZADA_TRANSACTION_LOOKBACK_DAYS = 90
LAZADA_TRANSACTION_MAX_PAGES = 20
LAZADA_TRANSACTION_PAGE_SIZE = 100
LAZADA_OAUTH_STATE_TTL_MINUTES = 10
LAZADA_TOKEN_REFRESH_MARGIN_HOURS = 24

# Response `code` values Lazada is known to return for an invalid/expired
# access token. Verify against the sandbox and extend this set as needed.
LAZADA_TOKEN_ERROR_CODES = {
    "IllegalAccessToken",
    "InvalidAccessToken",
    "AccessTokenExpired",
}


class PartnerLazadaIntegration(models.Model):
    _inherit = "partner"

    lazada_enabled = fields.Boolean(string="Lazada Enabled", default=False, copy=False, tracking=True)
    lazada_seller_id = fields.Char(string="Lazada Seller ID", copy=False)
    lazada_country = fields.Char(string="Lazada Country", copy=False, default="TH")
    lazada_access_token = fields.Char(string="Lazada Access Token", copy=False, password="True")
    lazada_access_token_expires_at = fields.Datetime(string="Lazada Access Token Expires At", copy=False)
    lazada_refresh_token = fields.Char(string="Lazada Refresh Token", copy=False, password="True")
    lazada_refresh_token_expires_at = fields.Datetime(string="Lazada Refresh Token Expires At", copy=False)
    lazada_flat_point_value = fields.Float(
        string="Lazada Flat Point Value",
        default=0,
        help="แต้มที่ให้แบบคงที่ต่อ order เมื่อ item ที่ delivered ไม่มี field ราคา",
    )
    lazada_oauth_state = fields.Char(string="Lazada OAuth State", copy=False)
    lazada_oauth_state_expires_at = fields.Datetime(string="Lazada OAuth State Expires At", copy=False)

    # ------------------------------------------------------------------
    # App-level credentials (shared across all tenants, not per-partner)
    # ------------------------------------------------------------------
    @api.model
    def _get_lazada_app_credentials(self):
        app_key = os.getenv("LAZADA_APP_KEY")
        app_secret = os.getenv("LAZADA_APP_SECRET")
        if not app_key or not app_secret:
            raise ValidationError("ยังไม่ได้ตั้งค่า LAZADA_APP_KEY/LAZADA_APP_SECRET")
        return app_key, app_secret

    # ------------------------------------------------------------------
    # Readiness / token validity
    # ------------------------------------------------------------------
    def _ensure_lazada_ready(self):
        self.ensure_one()
        if not self.lazada_enabled or not self.lazada_access_token:
            raise ValidationError("ยังไม่ได้เชื่อมต่อ Lazada กรุณาเชื่อมต่อร้านค้าก่อน")

    def _lazada_has_valid_access_token(self):
        self.ensure_one()
        if not self.lazada_access_token or not self.lazada_access_token_expires_at:
            return False
        return self.lazada_access_token_expires_at > fields.Datetime.now() + timedelta(minutes=1)

    # ------------------------------------------------------------------
    # Signing / generic call
    # ------------------------------------------------------------------
    def _lazada_sign_and_send(self, method, base_url, api_path, params):
        app_key, app_secret = self._get_lazada_app_credentials()
        full_params = dict(params or {})
        full_params.update({
            "app_key": app_key,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
        })
        full_params["sign"] = build_lazada_signature(api_path, full_params, app_secret)

        try:
            if method.upper() == "GET":
                response = requests.get(f"{base_url}{api_path}", params=full_params, timeout=30)
            else:
                response = requests.post(f"{base_url}{api_path}", data=full_params, timeout=30)
        except requests.RequestException as error:
            _logger.warning("Lazada %s %s failed for partner %s: %s", method, api_path, self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ Lazada API ได้") from error

        try:
            payload = response.json()
        except ValueError as error:
            raise ValidationError("Lazada API ตอบกลับข้อมูลไม่ถูกต้อง") from error
        if not isinstance(payload, dict):
            raise ValidationError("Lazada API ตอบกลับข้อมูลไม่ถูกต้อง")
        return response, payload

    def _is_lazada_token_error(self, response, payload):
        if response.status_code == 401:
            return True
        code = str(payload.get("code") or "")
        return code in LAZADA_TOKEN_ERROR_CODES

    def _lazada_call(self, method, api_path, params=None, force_refresh=False):
        """Generic authenticated Lazada Open Platform call, any endpoint."""
        self.ensure_one()
        self._ensure_lazada_ready()

        if force_refresh or not self._lazada_has_valid_access_token():
            self._lazada_refresh_access_token()

        call_params = dict(params or {})
        call_params["access_token"] = self.lazada_access_token
        response, payload = self._lazada_sign_and_send(method, LAZADA_API_BASE_URL, api_path, call_params)

        if self._is_lazada_token_error(response, payload):
            if force_refresh:
                raise ValidationError("Lazada access token ไม่ถูกต้อง กรุณาเชื่อมต่อร้านค้าใหม่")
            self._lazada_refresh_access_token()
            return self._lazada_call(method, api_path, params=params, force_refresh=True)

        code = str(payload.get("code") or "0")
        if code not in ("0", ""):
            message = payload.get("message") or f"Lazada API error (code={code})"
            raise ValidationError(message)

        return payload

    def fetch_lazada_transactions(self, status=None, created_after=None, created_before=None,
                                   offset=0, limit=LAZADA_TRANSACTION_PAGE_SIZE, **extra_params):
        self.ensure_one()
        params = {"offset": offset, "limit": min(limit, LAZADA_TRANSACTION_PAGE_SIZE)}
        if self.lazada_seller_id:
            params["seller_id"] = self.lazada_seller_id
        if status:
            params["status"] = status
        if created_after:
            params["created_after"] = created_after
        if created_before:
            params["created_before"] = created_before
        params.update(extra_params)
        payload = self._lazada_call("GET", "/partner/transaction", params=params)
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("module") or data.get("list") or []
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # OAuth: connect
    # ------------------------------------------------------------------
    def _lazada_callback_url(self):
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        return f"{base_url}/api/integrations/lazada/oauth/callback"

    def start_lazada_oauth_for_api(self):
        self.ensure_one()
        app_key, _app_secret = self._get_lazada_app_credentials()

        state = secrets.token_urlsafe(32)
        self.write({
            "lazada_oauth_state": state,
            "lazada_oauth_state_expires_at": fields.Datetime.now() + timedelta(minutes=LAZADA_OAUTH_STATE_TTL_MINUTES),
        })

        query = urlencode({
            "response_type": "code",
            "force_auth": "true",
            "redirect_uri": self._lazada_callback_url(),
            "client_id": app_key,
            "state": state,
        })
        return {"authorize_url": f"https://auth.lazada.com/oauth/authorize?{query}"}

    @api.model
    def get_partner_from_lazada_oauth_state(self, state):
        state = (state or "").strip()
        if not state:
            return self.browse()
        return self.sudo().search([
            ("lazada_oauth_state", "=", state),
            ("lazada_oauth_state_expires_at", ">=", fields.Datetime.now()),
        ], limit=1)

    def complete_lazada_oauth(self, code):
        self.ensure_one()
        code = (code or "").strip()
        if not code:
            raise ValidationError("Lazada OAuth code หายไป")

        _response, payload = self._lazada_sign_and_send(
            "GET", LAZADA_AUTH_BASE_URL, "/auth/token/create", {"code": code},
        )
        self._write_lazada_token_payload(payload)
        self.write({
            "lazada_enabled": True,
            "lazada_oauth_state": False,
            "lazada_oauth_state_expires_at": False,
        })

    def _write_lazada_token_payload(self, payload):
        self.ensure_one()
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token or not refresh_token:
            message = payload.get("message") or "Lazada ไม่ส่ง access token กลับมา"
            raise ValidationError(message)

        now = fields.Datetime.now()
        vals = {
            "lazada_access_token": access_token,
            "lazada_access_token_expires_at": now + timedelta(seconds=int(payload.get("expires_in") or 0)),
            "lazada_refresh_token": refresh_token,
        }
        refresh_expires_in = payload.get("refresh_expires_in")
        if refresh_expires_in:
            vals["lazada_refresh_token_expires_at"] = now + timedelta(seconds=int(refresh_expires_in))

        country_user_info = payload.get("country_user_info")
        if isinstance(country_user_info, list) and country_user_info:
            first = country_user_info[0]
            if isinstance(first, dict):
                vals["lazada_seller_id"] = first.get("seller_id") or first.get("user_id") or self.lazada_seller_id
                vals["lazada_country"] = first.get("country") or self.lazada_country

        self.write(vals)

    def _lazada_refresh_access_token(self):
        self.ensure_one()
        if not self.lazada_refresh_token:
            raise ValidationError("ไม่พบ Lazada refresh token กรุณาเชื่อมต่อร้านค้าใหม่")
        if (
            self.lazada_refresh_token_expires_at
            and self.lazada_refresh_token_expires_at <= fields.Datetime.now()
        ):
            self.write({"lazada_enabled": False})
            raise ValidationError("Lazada refresh token หมดอายุ กรุณาเชื่อมต่อร้านค้าใหม่")

        _response, payload = self._lazada_sign_and_send(
            "GET", LAZADA_AUTH_BASE_URL, "/auth/token/refresh",
            {"refresh_token": self.lazada_refresh_token},
        )
        self._write_lazada_token_payload(payload)

    def disable_lazada_for_api(self):
        self.ensure_one()
        self.write({"lazada_enabled": False})
        return self.serialize_lazada_status()

    def serialize_lazada_status(self):
        self.ensure_one()
        token = self.lazada_access_token or ""
        return {
            "enabled": bool(self.lazada_enabled),
            "configured": bool(token),
            "seller_id": self.lazada_seller_id or None,
            "country": self.lazada_country or None,
            "access_token_expires_at": (
                fields.Datetime.to_string(self.lazada_access_token_expires_at)
                if self.lazada_access_token_expires_at
                else False
            ),
            "token_masked": f"...{token[-4:]}" if len(token) >= 4 else bool(token),
        }

    # ------------------------------------------------------------------
    # Token refresh cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_refresh_lazada_tokens(self):
        soon = fields.Datetime.now() + timedelta(hours=LAZADA_TOKEN_REFRESH_MARGIN_HOURS)
        partners = self.sudo().search([
            ("lazada_enabled", "=", True),
            ("lazada_access_token_expires_at", "<=", soon),
        ])
        for partner in partners:
            try:
                partner._lazada_refresh_access_token()
            except ValidationError as error:
                _logger.warning("Lazada token refresh failed for partner %s: %s", partner.id, error)
            except Exception:
                _logger.exception("Lazada token refresh crashed for partner %s", partner.id)
