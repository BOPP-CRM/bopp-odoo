import math
import os
import secrets
from datetime import datetime, timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

OMISELL_API_BASE_URL = "https://api.omisell.com"
OMISELL_AUTH_TOKEN_PATH = "/api/v2/auth/token/get/"
OMISELL_ELIGIBLE_STATUS_IDS = {300, 350, 360, 400, 460, 500, 600, 900}
OMISELL_ELIGIBLE_STATUS_NAMES = {
    "approved",
    "awaiting generate label",
    "processing",
    "ready to ship",
    "shipped",
    "delivering",
    "completed",
}


class PartnerOmisellIntegration(models.Model):
    _inherit = "partner"

    omisell_enabled = fields.Boolean(
        string="Omisell Enabled",
        default=False,
        tracking=True,
    )
    omisell_webhook_token = fields.Char(
        string="Omisell Webhook Token",
        copy=False,
        index=True,
    )
    omisell_webhook_secret = fields.Char(
        string="Omisell Webhook Secret",
        copy=False,
    )
    omisell_api_key = fields.Char(
        string="Omisell API Key",
        copy=False,
        password="True",
    )
    omisell_api_secret = fields.Char(
        string="Omisell API Secret",
        copy=False,
        password="True",
    )
    omisell_access_token = fields.Char(string="Omisell Access Token", copy=False)
    omisell_access_token_expired_at = fields.Datetime(
        string="Omisell Access Token Expired At",
        copy=False,
    )
    omisell_seller_id = fields.Char(string="Omisell Seller ID", copy=False)
    omisell_country = fields.Char(string="Omisell Country", copy=False, default="TH")
    omisell_api_base_url = fields.Char(
        string="Omisell API Base URL",
        copy=False,
        default=OMISELL_API_BASE_URL,
    )
    omisell_order_ids = fields.One2many(
        "partner.omisell.order",
        "partner_id",
        string="Omisell Orders",
    )

    _sql_constraints = [
        (
            "partner_omisell_webhook_token_uniq",
            "unique(omisell_webhook_token)",
            "Omisell webhook token must be unique.",
        ),
    ]

    def write(self, vals):
        token_related_fields = {"omisell_api_key", "omisell_api_secret", "omisell_api_base_url"}
        if token_related_fields.intersection(vals.keys()):
            vals = {
                **vals,
                "omisell_access_token": False,
                "omisell_access_token_expired_at": False,
            }
        result = super().write(vals)
        if vals.get("omisell_enabled"):
            for partner in self:
                partner._validate_omisell_configuration()
                partner._ensure_omisell_credentials()
        return result

    @api.model
    def _generate_omisell_webhook_token(self):
        for _ in range(10):
            token = secrets.token_urlsafe(32)
            if not self.search([("omisell_webhook_token", "=", token)], limit=1):
                return token
        raise ValidationError("Unable to generate Omisell webhook token.")

    @api.model
    def _generate_omisell_secret(self):
        return secrets.token_urlsafe(32)

    @api.model
    def _parse_omisell_status_id(self, value):
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    @api.model
    def get_partner_from_omisell_token(self, token):
        token = (token or "").strip()
        if not token:
            return self.browse()

        return self.sudo().search([
            ("omisell_webhook_token", "=", token),
            ("omisell_enabled", "=", True),
            ("active", "=", True),
        ], limit=1)

    def _get_omisell_webhook_base_url(self):
        self.ensure_one()
        if not self.omisell_webhook_token:
            return False

        portal_base = (os.getenv("PORTAL_FRONTEND_PATH") or "").rstrip("/")
        if portal_base:
            return f"{portal_base}/api/integrations/omisell/{self.omisell_webhook_token}"

        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        if not base_url:
            return False
        return f"{base_url}/api/integrations/omisell/{self.omisell_webhook_token}"

    def _validate_omisell_configuration(self):
        self.ensure_one()
        missing_fields = []
        if not (self.omisell_api_key or "").strip():
            missing_fields.append("API Key")
        if not (self.omisell_api_secret or "").strip():
            missing_fields.append("API Secret")
        if not (self.omisell_seller_id or "").strip():
            missing_fields.append("Seller ID")
        if missing_fields:
            missing_text = ", ".join(missing_fields)
            raise ValidationError(f"Missing Omisell configuration: {missing_text}")

    def _ensure_omisell_credentials(self):
        self.ensure_one()
        vals = {}
        if not self.omisell_webhook_token:
            vals["omisell_webhook_token"] = self._generate_omisell_webhook_token()
        if not self.omisell_webhook_secret:
            vals["omisell_webhook_secret"] = self._generate_omisell_secret()
        if not self.omisell_country:
            vals["omisell_country"] = "TH"
        if not self.omisell_api_base_url:
            vals["omisell_api_base_url"] = OMISELL_API_BASE_URL
        if vals:
            self.write(vals)

    def enable_omisell_for_api(self):
        self.ensure_one()
        self._validate_omisell_configuration()
        self._ensure_omisell_credentials()
        self.write({"omisell_enabled": True})
        return self.serialize_omisell_status()

    def disable_omisell_for_api(self):
        self.ensure_one()
        self.write({"omisell_enabled": False})
        return self.serialize_omisell_status()

    def regenerate_omisell_secret_for_api(self):
        self.ensure_one()
        if not self.omisell_webhook_token:
            raise ValidationError("Enable Omisell integration first.")
        self.write({
            "omisell_webhook_secret": self._generate_omisell_secret(),
        })
        return self.serialize_omisell_status()

    def serialize_omisell_status(self):
        self.ensure_one()
        webhook_url = self._get_omisell_webhook_base_url()
        return {
            "enabled": bool(self.omisell_enabled),
            "configured": bool(
                self.omisell_webhook_token
                and self.omisell_webhook_secret
                and self.omisell_api_key
                and self.omisell_api_secret
                and self.omisell_seller_id
            ),
            "webhook_url": webhook_url or None,
            "authorization": self.omisell_webhook_secret or None,
            "seller_id": self.omisell_seller_id or None,
            "country": self.omisell_country or None,
            "api_base_url": self.omisell_api_base_url or OMISELL_API_BASE_URL,
            "has_api_key": bool(self.omisell_api_key),
            "has_api_secret": bool(self.omisell_api_secret),
            "access_token_expired_at": (
                fields.Datetime.to_string(self.omisell_access_token_expired_at)
                if self.omisell_access_token_expired_at
                else False
            ),
        }

    def validate_omisell_request_headers(self, headers, payload=None):
        self.ensure_one()
        headers = headers or {}
        payload = payload or {}

        def _get_header(name):
            return (
                headers.get(name)
                or headers.get(name.lower())
                or headers.get(name.upper())
                or ""
            ).strip()

        authorization = _get_header("Authorization")
        if authorization != (self.omisell_webhook_secret or ""):
            return False

        incoming_seller = _get_header("Seller-ID")
        if incoming_seller and self.omisell_seller_id:
            if incoming_seller != (self.omisell_seller_id or "").strip():
                return False

        incoming_country = _get_header("Country")
        if incoming_country and self.omisell_country:
            if incoming_country.upper() != (self.omisell_country or "").strip().upper():
                return False

        payload_seller_id = payload.get("seller_id")
        if payload_seller_id and self.omisell_seller_id:
            if str(payload_seller_id).strip() != (self.omisell_seller_id or "").strip():
                return False

        return True

    @api.model
    def _normalize_omisell_phone(self, phone):
        phone = (phone or "").strip()
        if not phone:
            return ""
        digits = "".join(char for char in phone if char.isdigit())
        if digits.startswith("66") and len(digits) >= 10:
            return f"0{digits[2:]}"
        if digits.startswith("0"):
            return digits
        return phone

    def find_user_from_omisell_order_detail(self, order_detail):
        self.ensure_one()
        user_model = self.env["crm.user"].sudo()
        domain_base = [("partner_id", "=", self.id), ("active", "=", True)]
        receiver = order_detail.get("receiver") if isinstance(order_detail.get("receiver"), dict) else {}

        phone = self._normalize_omisell_phone(receiver.get("phone"))
        email = (receiver.get("email") or "").strip().lower()

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
    def _parse_omisell_amount(value):
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(amount, 0.0)

    @api.model
    def get_omisell_payment_information(self, order_detail):
        payment_information = order_detail.get("payment_information")
        return payment_information if isinstance(payment_information, list) else []

    def get_omisell_payment_status(self, order_detail):
        statuses = []
        for payment in self.get_omisell_payment_information(order_detail):
            status = (payment.get("transaction_status") or "").strip()
            if status and status not in statuses:
                statuses.append(status)
        return ", ".join(statuses)

    def get_omisell_payment_method(self, order_detail):
        methods = []
        for payment in self.get_omisell_payment_information(order_detail):
            method = (
                payment.get("payment_method_name")
                or payment.get("payment_method")
                or payment.get("origin_payment_method")
                or ""
            ).strip()
            if method and method not in methods:
                methods.append(method)
        return ", ".join(methods)

    def get_omisell_order_amount(self, order_detail):
        total = sum(
            self._parse_omisell_amount(payment.get("transaction_amount"))
            for payment in self.get_omisell_payment_information(order_detail)
        )
        if total > 0:
            return total

        parcels = order_detail.get("parcels") if isinstance(order_detail.get("parcels"), list) else []
        inventory_total = 0.0
        for parcel in parcels:
            inventory_items = parcel.get("inventory_items") if isinstance(parcel.get("inventory_items"), list) else []
            for item in inventory_items:
                inventory_total += self._parse_omisell_amount(item.get("sale_price")) * float(item.get("quantity") or 0)
        if inventory_total > 0:
            return inventory_total

        catalogue_total = 0.0
        for parcel in parcels:
            catalogue_items = parcel.get("catalogue_items") if isinstance(parcel.get("catalogue_items"), list) else []
            for item in catalogue_items:
                catalogue_total += self._parse_omisell_amount(item.get("discounted_price")) * float(item.get("quantity") or 0)
        return catalogue_total

    def is_omisell_order_eligible_for_points(self, payload, order_detail):
        self.ensure_one()
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        status_id = self._parse_omisell_status_id(
            order_detail.get("status_id") or payload_data.get("status_id")
        )
        if status_id in OMISELL_ELIGIBLE_STATUS_IDS:
            return True

        status_name = (
            order_detail.get("status_name")
            or payload_data.get("status_name")
            or ""
        ).strip().lower()
        return status_name in OMISELL_ELIGIBLE_STATUS_NAMES

    def _has_valid_omisell_access_token(self):
        self.ensure_one()
        if not self.omisell_access_token or not self.omisell_access_token_expired_at:
            return False
        expired_at = fields.Datetime.to_datetime(self.omisell_access_token_expired_at)
        return bool(expired_at and expired_at > fields.Datetime.now() + timedelta(minutes=1))

    def _get_omisell_auth_url(self):
        self.ensure_one()
        base_url = (self.omisell_api_base_url or OMISELL_API_BASE_URL).rstrip("/")
        return f"{base_url}{OMISELL_AUTH_TOKEN_PATH}"

    def _request_omisell_access_token(self):
        self.ensure_one()
        self._validate_omisell_configuration()

        auth_url = self._get_omisell_auth_url()
        payload = {
            "api_key": (self.omisell_api_key or "").strip(),
            "api_secret": (self.omisell_api_secret or "").strip(),
        }

        try:
            response = requests.post(
                auth_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
        except requests.RequestException as error:
            raise ValidationError(f"Unable to connect to Omisell auth API: {error}") from error

        try:
            response_payload = response.json()
        except ValueError as error:
            raise ValidationError("Invalid response from Omisell auth API.") from error

        if response.status_code >= 400:
            message = response_payload.get("messages") if isinstance(response_payload, dict) else False
            raise ValidationError(message or f"Omisell auth API returned HTTP {response.status_code}.")

        if not isinstance(response_payload, dict):
            raise ValidationError("Invalid response from Omisell auth API.")

        if response_payload.get("error"):
            raise ValidationError(response_payload.get("messages") or "Omisell auth API returned an error.")

        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise ValidationError("Invalid Omisell access token response.")

        access_token = (data.get("token") or "").strip()
        expired_time = data.get("expired_time")
        if not access_token or not expired_time:
            raise ValidationError("Omisell auth response is missing token data.")

        try:
            expired_at = datetime.utcfromtimestamp(float(expired_time))
        except Exception as error:
            raise ValidationError("Invalid expired_time from Omisell auth response.") from error

        self.write({
            "omisell_access_token": access_token,
            "omisell_access_token_expired_at": fields.Datetime.to_string(expired_at),
        })
        return access_token

    def _get_omisell_request_headers(self, force_refresh=False):
        self.ensure_one()
        self._validate_omisell_configuration()

        access_token = self.omisell_access_token if not force_refresh else False
        if not access_token or force_refresh or not self._has_valid_omisell_access_token():
            access_token = self._request_omisell_access_token()

        headers = {
            "Authorization": f"Omi {access_token}",
            "Content-Type": "application/json",
        }
        if self.omisell_seller_id:
            headers["Seller-ID"] = (self.omisell_seller_id or "").strip()
        if self.omisell_country:
            headers["Country"] = (self.omisell_country or "").strip().upper()
        return headers

    def fetch_omisell_order_detail(self, omisell_order_number):
        self.ensure_one()
        omisell_order_number = (omisell_order_number or "").strip()
        if not omisell_order_number:
            raise ValidationError("Missing Omisell order number.")

        base_url = (self.omisell_api_base_url or OMISELL_API_BASE_URL).rstrip("/")
        url = f"{base_url}/api/v2/public/order/{omisell_order_number}"

        try:
            response = requests.get(
                url,
                headers=self._get_omisell_request_headers(),
                timeout=30,
            )
            if response.status_code == 401:
                response = requests.get(
                    url,
                    headers=self._get_omisell_request_headers(force_refresh=True),
                    timeout=30,
                )
        except requests.RequestException as error:
            raise ValidationError(f"Unable to connect to Omisell API: {error}") from error

        try:
            response_payload = response.json()
        except ValueError as error:
            raise ValidationError("Invalid response from Omisell API.") from error

        if response.status_code >= 400:
            message = response_payload.get("messages") if isinstance(response_payload, dict) else False
            raise ValidationError(message or f"Omisell API returned HTTP {response.status_code}.")

        if not isinstance(response_payload, dict):
            raise ValidationError("Invalid response from Omisell API.")

        if response_payload.get("error"):
            raise ValidationError(response_payload.get("messages") or "Omisell API returned an error.")

        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise ValidationError("Invalid Omisell order detail response.")
        return data

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

    def process_omisell_webhook(self, payload):
        self.ensure_one()
        order_model = self.env["partner.omisell.order"].sudo()
        return order_model.process_webhook(self, payload)
