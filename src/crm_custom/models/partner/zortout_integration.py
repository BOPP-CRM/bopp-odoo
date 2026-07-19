import logging
import math
import os
import secrets

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

ZORTOUT_API_BASE_URL = "https://open-api.zortout.com/v4"
ZORTOUT_WEBHOOK_URL_FIELDS = (
    "addproducturl",
    "updateproducturl",
    "deleteproducturl",
    "updatequantityurl",
    "addorderurl",
    "updateorderurl",
    "deleteorderurl",
    "updateordertrackingurl",
    "updateorderpaymenturl",
    "addpurchaseurl",
    "updatepurchaseurl",
    "updatepurchasepaymenturl",
    "addreturnorderurl",
    "updatereturnorderurl",
    "updatereturnorderpaymenturl",
    "addreturnpurchaseurl",
    "updatereturnpurchaseurl",
    "updatereturnpurchasepaymenturl",
    "addtransferurl",
    "updatetransferurl",
    "deletewarehouseurl",
    "updatewarehouseurl",
    "addwarehouseurl",
    "deletecontacturl",
    "updatecontacturl",
    "addcontacturl",
)
ZORTOUT_ORDER_WEBHOOK_FIELDS = (
    "addorderurl",
    "updateorderurl",
    "deleteorderurl",
)


class PartnerZortoutIntegration(models.Model):
    _inherit = "partner"

    zortout_enabled = fields.Boolean(
        string="Zortout Enabled",
        default=False,
        tracking=True,
    )
    zortout_webhook_token = fields.Char(
        string="Zortout Webhook Token",
        copy=False,
        index=True,
    )
    zortout_key1 = fields.Char(string="Zortout Key 1", copy=False)
    zortout_key2 = fields.Char(string="Zortout Key 2", copy=False)
    zortout_key3 = fields.Char(string="Zortout Key 3", copy=False)
    zortout_store_name = fields.Char(string="Zortout Store Name", copy=False)
    zortout_api_key = fields.Char(string="Zortout API Key", copy=False)
    zortout_api_secret = fields.Char(string="Zortout API Secret", copy=False)
    zortout_webhook_synced = fields.Boolean(
        string="Zortout Webhook Synced",
        default=False,
        tracking=True,
    )
    zortout_order_ids = fields.One2many(
        "partner.zortout.order",
        "partner_id",
        string="Zortout Orders",
    )

    _sql_constraints = [
        (
            "partner_zortout_webhook_token_uniq",
            "unique(zortout_webhook_token)",
            "Zortout webhook token must be unique.",
        ),
    ]

    @api.model
    def _generate_zortout_webhook_token(self):
        for _ in range(10):
            token = secrets.token_urlsafe(32)
            if not self.search([("zortout_webhook_token", "=", token)], limit=1):
                return token
        raise ValidationError("Unable to generate Zortout webhook token.")

    @api.model
    def _generate_zortout_key(self):
        return secrets.token_urlsafe(24)

    @api.model
    def get_partner_from_zortout_token(self, token):
        token = (token or "").strip()
        if not token:
            return self.browse()

        return self.sudo().search([
            ("zortout_webhook_token", "=", token),
            ("zortout_enabled", "=", True),
            ("active", "=", True),
        ], limit=1)

    def _get_zortout_webhook_base_url(self):
        self.ensure_one()
        if not self.zortout_webhook_token:
            return False

        portal_base = (os.getenv("PORTAL_FRONTEND_PATH") or "").rstrip("/")
        if portal_base:
            return f"{portal_base}/api/integrations/zortout/{self.zortout_webhook_token}"

        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        if not base_url:
            return False
        return f"{base_url}/api/integrations/zortout/{self.zortout_webhook_token}"

    def _ensure_zortout_credentials(self):
        self.ensure_one()
        vals = {}
        if not self.zortout_webhook_token:
            vals["zortout_webhook_token"] = self._generate_zortout_webhook_token()
        if not self.zortout_key1:
            vals["zortout_key1"] = self._generate_zortout_key()
        if not self.zortout_key2:
            vals["zortout_key2"] = self._generate_zortout_key()
        if not self.zortout_key3:
            vals["zortout_key3"] = self._generate_zortout_key()
        if vals:
            self.write(vals)

    def enable_zortout_for_api(self):
        self.ensure_one()
        self._ensure_zortout_credentials()
        self.write({"zortout_enabled": True})
        return self.serialize_zortout_status()

    def disable_zortout_for_api(self):
        self.ensure_one()
        self.write({"zortout_enabled": False})
        return self.serialize_zortout_status()

    def regenerate_zortout_keys_for_api(self):
        self.ensure_one()
        if not self.zortout_webhook_token:
            raise ValidationError("Enable Zortout integration first.")
        self.write({
            "zortout_key1": self._generate_zortout_key(),
            "zortout_key2": self._generate_zortout_key(),
            "zortout_key3": self._generate_zortout_key(),
        })
        if self.zortout_api_key and self.zortout_api_secret:
            self._sync_zortout_webhook_with_stored_credentials()
        return self.serialize_zortout_status()

    def connect_zortout_for_api(self, storename, apikey, apisecret):
        self.ensure_one()
        storename = (storename or "").strip()
        apikey = (apikey or "").strip()
        apisecret = (apisecret or "").strip()
        if not storename:
            raise ValidationError("กรุณาระบุ Store Name จาก ZORT")
        if not apikey or not apisecret:
            raise ValidationError("กรุณาระบุ API Key และ API Secret")

        self._ensure_zortout_credentials()
        self._verify_zortout_credentials(storename, apikey, apisecret)
        self.write({
            "zortout_store_name": storename,
            "zortout_api_key": apikey,
            "zortout_api_secret": apisecret,
        })
        self._sync_zortout_webhook(apikey, apisecret, storename)
        self.write({
            "zortout_enabled": True,
            "zortout_webhook_synced": True,
        })
        return self.serialize_zortout_status()

    def resync_zortout_webhook_for_api(
        self,
        storename=None,
        apikey=None,
        apisecret=None,
    ):
        self.ensure_one()
        storename = (storename or self.zortout_store_name or "").strip()
        apikey = (apikey or self.zortout_api_key or "").strip()
        apisecret = (apisecret or self.zortout_api_secret or "").strip()
        if not storename:
            raise ValidationError("กรุณาระบุ Store Name จาก ZORT")
        if not apikey or not apisecret:
            raise ValidationError("กรุณาระบุ API Key และ API Secret")

        self._ensure_zortout_credentials()
        self._verify_zortout_credentials(storename, apikey, apisecret)
        self.write({
            "zortout_store_name": storename,
            "zortout_api_key": apikey,
            "zortout_api_secret": apisecret,
        })
        self._sync_zortout_webhook(apikey, apisecret, storename)
        self.write({
            "zortout_enabled": True,
            "zortout_webhook_synced": True,
        })
        return self.serialize_zortout_status()

    def serialize_zortout_status(self):
        self.ensure_one()
        base_url = self._get_zortout_webhook_base_url()
        return {
            "enabled": bool(self.zortout_enabled),
            "configured": bool(self.zortout_webhook_token and self.zortout_key1),
            "webhook_synced": bool(self.zortout_webhook_synced),
            "api_credentials_configured": bool(
                self.zortout_api_key and self.zortout_api_secret
            ),
            "store_name": self.zortout_store_name or None,
            "webhook_base_url": base_url or None,
            "addorder_url": base_url or None,
            "updateorder_url": base_url or None,
            "deleteorder_url": base_url or None,
            "key1": self.zortout_key1 or None,
            "key2": self.zortout_key2 or None,
            "key3": self.zortout_key3 or None,
        }

    @api.model
    def _zortout_request_headers(self, storename, apikey, apisecret):
        return {
            "storename": storename,
            "apikey": apikey,
            "apisecret": apisecret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @api.model
    def _is_zortout_webhook_config(self, data):
        if not isinstance(data, dict):
            return False
        return any(
            marker in data
            for marker in ("key1", "addorderurl", "updateorderurl", "hookVersion")
        )

    @api.model
    def _parse_zortout_response(self, response):
        try:
            data = response.json()
        except ValueError:
            return False, "Zortout ตอบกลับข้อมูลไม่ถูกต้อง", {}

        if not isinstance(data, dict):
            return False, "Zortout ตอบกลับข้อมูลไม่ถูกต้อง", {}

        res_code = data.get("resCode")
        if res_code is None and isinstance(data.get("res"), dict):
            res_code = data["res"].get("resCode")

        if str(res_code) == "200":
            return True, data.get("resDesc") or "Success", data

        if response.ok and self._is_zortout_webhook_config(data):
            return True, "Success", data

        res_desc = data.get("resDesc")
        if not res_desc and isinstance(data.get("res"), dict):
            res_desc = data["res"].get("resDesc")
        if not res_desc:
            res_desc = "Zortout API request failed"
        return False, res_desc, data

    @api.model
    def _is_zortout_missing_webhook_message(self, message):
        normalized = (message or "").strip().lower()
        return normalized in {
            "invalid webhook",
            "webhook not found",
        }

    def _verify_zortout_credentials(self, storename, apikey, apisecret):
        self.ensure_one()
        ok, message, _data = self._get_zortout_webhook(apikey, apisecret, storename)
        if ok:
            return

        if self._is_zortout_missing_webhook_message(message):
            return

        if message:
            raise ValidationError(
                f"ไม่สามารถยืนยัน Zortout credentials ได้: {message}"
            )
        raise ValidationError(
            "ไม่สามารถยืนยัน Zortout credentials ได้ "
            "กรุณาตรวจสอบ Store Name, API Key และ API Secret"
        )

    def _get_zortout_webhook(self, apikey, apisecret, storename):
        self.ensure_one()
        try:
            response = requests.get(
                f"{ZORTOUT_API_BASE_URL}/Webhook/GetWebhook",
                headers=self._zortout_request_headers(storename, apikey, apisecret),
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning("Zortout GetWebhook failed for partner %s: %s", self.id, error)
            return False, "ไม่สามารถเชื่อมต่อ Zortout API ได้", {}

        return self._parse_zortout_response(response)

    def _sync_zortout_webhook_with_stored_credentials(self):
        self.ensure_one()
        if not self.zortout_api_key or not self.zortout_api_secret:
            raise ValidationError("ยังไม่ได้บันทึก Zortout API credentials")
        if not self.zortout_store_name:
            raise ValidationError("ยังไม่ได้บันทึก Store Name จาก ZORT")
        self._sync_zortout_webhook(
            self.zortout_api_key,
            self.zortout_api_secret,
            self.zortout_store_name,
        )
        self.write({"zortout_webhook_synced": True})

    def _sync_zortout_webhook(self, apikey, apisecret, storename):
        self.ensure_one()
        webhook_url = self._get_zortout_webhook_base_url()
        if not webhook_url:
            raise ValidationError("ไม่พบ Webhook URL ของระบบ")

        ok, _message, current = self._get_zortout_webhook(apikey, apisecret, storename)
        payload = {
            "key1": self.zortout_key1,
            "key2": self.zortout_key2,
            "key3": self.zortout_key3,
        }
        if ok:
            for field in ZORTOUT_WEBHOOK_URL_FIELDS:
                value = current.get(field)
                if value not in (None, "", False):
                    payload[field] = value

        for field in ZORTOUT_ORDER_WEBHOOK_FIELDS:
            payload[field] = webhook_url

        try:
            response = requests.post(
                f"{ZORTOUT_API_BASE_URL}/Webhook/UpdateWebhook",
                headers=self._zortout_request_headers(storename, apikey, apisecret),
                json=payload,
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning("Zortout UpdateWebhook failed for partner %s: %s", self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ Zortout API ได้") from error

        ok, message, _data = self._parse_zortout_response(response)
        if not ok:
            raise ValidationError(message or "ไม่สามารถตั้งค่า Webhook ใน Zortout ได้")

    def validate_zortout_request_headers(self, headers):
        self.ensure_one()
        headers = headers or {}

        def _get_header(name):
            return (headers.get(name) or headers.get(name.lower()) or headers.get(name.upper()) or "").strip()

        key1 = _get_header("key1")
        authorization = _get_header("Authorization")
        if authorization.lower().startswith("basic "):
            basic_key = authorization[6:].strip()
            if basic_key and not key1:
                key1 = basic_key

        if not key1 or key1 != (self.zortout_key1 or ""):
            return False

        incoming_key2 = _get_header("key2")
        if incoming_key2 and self.zortout_key2 and incoming_key2 != self.zortout_key2:
            return False

        incoming_key3 = _get_header("key3")
        if incoming_key3 and self.zortout_key3 and incoming_key3 != self.zortout_key3:
            return False

        return True

    @api.model
    def _normalize_zortout_phone(self, phone):
        phone = (phone or "").strip()
        if not phone:
            return ""
        digits = "".join(char for char in phone if char.isdigit())
        if digits.startswith("66") and len(digits) >= 10:
            return f"0{digits[2:]}"
        if digits.startswith("0"):
            return digits
        return phone

    def find_user_from_zortout_payload(self, payload):
        self.ensure_one()
        user_model = self.env["crm.user"].sudo()
        domain_base = [("partner_id", "=", self.id), ("active", "=", True)]

        phone = self._normalize_zortout_phone(payload.get("customerphone"))
        email = (payload.get("customeremail") or "").strip().lower()

        if phone:
            user = user_model.search(domain_base + [("phone", "=", phone)], limit=1)
            if user:
                return user

        if email:
            user = user_model.search(domain_base + [("email", "=", email)], limit=1)
            if user:
                return user

        return user_model.browse()

    @staticmethod
    def _parse_zortout_amount(value):
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(amount, 0.0)

    @staticmethod
    def is_zortout_payment_successful(payload):
        payment_status = (payload.get("paymentstatus") or "").strip()
        return payment_status == "Paid"

    @staticmethod
    def is_zortout_order_voided(payload):
        status = (payload.get("status") or "").strip().lower()
        return status == "voided"

    def _get_spending_currency(self):
        self.ensure_one()
        return self.currency_ids.filtered("is_total_spending")[:1]

    def _get_default_point_currency(self):
        self.ensure_one()
        return self.currency_ids.filtered("is_default")[:1]

    @staticmethod
    def _calculate_reward_points(amount, convert_points):
        if amount <= 0 or convert_points <= 0:
            return 0
        return math.floor(amount / convert_points)

    def _get_user_convert_points(self, user):
        self.ensure_one()
        user._update_tier()
        tier = user.tier_id
        if not tier:
            tier = self.env["partner.tier"].search([
                ("partner_id", "=", self.id),
            ], order="min_spending asc", limit=1)
            if tier:
                user.tier_id = tier
        if not tier or tier.convert_points <= 0:
            return 0
        return tier.convert_points

    def process_zortout_webhook(self, method, payload):
        self.ensure_one()
        order_model = self.env["partner.zortout.order"].sudo()
        return order_model.process_webhook(self, method, payload)
