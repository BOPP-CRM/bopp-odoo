import math
import os
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError


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
        return self.serialize_zortout_status()

    def serialize_zortout_status(self):
        self.ensure_one()
        base_url = self._get_zortout_webhook_base_url()
        return {
            "enabled": bool(self.zortout_enabled),
            "configured": bool(self.zortout_webhook_token and self.zortout_key1),
            "webhook_base_url": base_url or None,
            "addorder_url": base_url or None,
            "updateorder_url": base_url or None,
            "key1": self.zortout_key1 or None,
            "key2": self.zortout_key2 or None,
            "key3": self.zortout_key3 or None,
        }

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
