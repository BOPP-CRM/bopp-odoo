import logging
import secrets
import string

import requests
import os

from datetime import timedelta
from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ....util.phone import is_valid_th_msisdn, normalize_th_msisdn

from .sms_otp_providers import GODYSMS_PROVIDER, THAIBULKSMS_PROVIDER

_logger = logging.getLogger(__name__)


class OTP(models.Model):
    _name = "crm.otp"
    _inherit = ["crm.sms.otp.provider.mixin"]
    _description = "OTP"
    _order = "create_date desc"
    _REF_ALPHABET = string.ascii_letters + string.digits

    recipient = fields.Char(string="Recipient")
    key = fields.Char(string="Key")
    key_id = fields.Char(string="Key Id")
    ref = fields.Char(string="Ref")
    otp = fields.Char(string="OTP")
    expiry = fields.Datetime(string="Expiry", default=fields.Datetime.now)
    is_sent = fields.Boolean(string="Is Sent")
    reason = fields.Char(string="Reason")
    sms_provider = fields.Selection(
        selection=[
            (GODYSMS_PROVIDER, "Gody SMS"),
            (THAIBULKSMS_PROVIDER, "ThaiBulkSMS"),
        ],
        string="SMS Provider",
        readonly=True,
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    type = fields.Selection(
        selection=[
            ("email", "Email"),
            ("phone", "Phone"),
        ],
        string="Type",
        required=True,
    )

    @api.model
    def generate_otp(self, recipient, otp_type, key, key_id, partner):
        valid_types = dict(self._fields["type"].selection)
        if otp_type not in valid_types:
            raise ValidationError("Invalid OTP type.")

        if otp_type == "phone":
            recipient = normalize_th_msisdn(recipient)
            if not is_valid_th_msisdn(recipient):
                raise ValidationError(
                    "Invalid Thai mobile phone number. Use 0XXXXXXXXX or 66XXXXXXXXX."
                )

        otp = self.create({
            "recipient": recipient,
            "type": otp_type,
            "expiry": fields.Datetime.now() + timedelta(minutes=15),
            "key": key,
            "key_id": key_id,
            "partner_id": partner.id,
        })

        if otp_type == "phone":
            otp_ref = otp.send_sms_otp()
            if otp_ref:
                otp.write({
                    "otp": otp_ref.get("transaction_id"),
                    "ref": otp_ref.get("ref"),
                })

        elif otp_type == "email":
            otp_code = f"{secrets.randbelow(1000000):06d}"
            otp_ref = "".join(secrets.choice(self._REF_ALPHABET) for _ in range(5))
            otp.write({
                "otp": otp_code,
                "ref": otp_ref,
            })
            otp.send_email_otp()

        return otp

    @api.model
    def has_sms_otp_credit(self):
        return self._provider_has_credit(GODYSMS_PROVIDER) or self._provider_has_credit(
            THAIBULKSMS_PROVIDER
        )

    def make_as_fail(self, reason: str):
        return self.write({
            "reason": reason,
            "is_sent": False
        })

    def make_as_send(self, reason: str):
        return self.write({
            "reason": reason,
            "is_sent": True
        })

    def send_email_otp(self):
        base_url = os.getenv("EMAIL_API_ENDPOINT", False)
        if not base_url:
            self.make_as_fail("EMAIL_API_ENDPOINT not configured.")
            return False

        api_token = os.getenv("EMAIL_API_TOKEN", False)
        if not api_token:
            self.make_as_fail("EMAIL_API_TOKEN not configured.")
            return False

        url = f"{base_url}/emails"

        payload = {
            "from": "BOPP <no-reply@bopp.digital>",
            "to": self.recipient,
            "template": {
                "id": "7bcb7701-6418-44b2-a79f-fb4665ddf8f2",
                "variables": {
                    "PARTNER_NAME": self.partner_id.name,
                    "OTP_REF": self.ref,
                    "OTP_CODE": self.otp,
                }
            }
        }
        headers = {
            "authorization": f"Bearer {api_token}",
            "content-type": "application/json"
        }

        response = requests.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            self.make_as_fail(f"failed to send email otp: {response.text}")
            return False

        self.make_as_send(response.text)
        return {
            "id": response.json().get("id", False),
        }

    def _attempt_send_sms_provider(self, provider):
        if provider == GODYSMS_PROVIDER:
            if not self._godysms_configured():
                return None, "godysms not configured."
            return self._send_godysms_otp()
        if provider == THAIBULKSMS_PROVIDER:
            if not self._thaibulksms_otp_configured():
                return None, "thaibulksms not configured."
            return self._send_thaibulksms_otp()
        return None, f"unknown sms provider: {provider}"

    def _other_sms_provider(self, provider):
        if provider == GODYSMS_PROVIDER:
            return THAIBULKSMS_PROVIDER
        if provider == THAIBULKSMS_PROVIDER:
            return GODYSMS_PROVIDER
        return None

    def _is_out_of_credit_failure(self, reason):
        text = (reason or "").lower()
        return "insufficient" in text and "credit" in text

    def send_sms_otp_via_provider(self, provider):
        """Send SMS OTP using a single provider (admin test tool; no weighted fallback)."""
        self.ensure_one()
        if self.type != "phone":
            raise ValidationError("SMS OTP can only be sent for phone type records.")

        self.write({"sms_provider": provider})
        result, detail = self._attempt_send_sms_provider(provider)
        if not result:
            self.make_as_fail(detail or "failed to send sms otp.")
            return False

        self.write({
            "otp": result.get("transaction_id"),
            "ref": result.get("ref"),
        })
        log_reason = f"[{provider}] {detail}"
        self.make_as_send(log_reason)
        _logger.info(
            "crm.otp SMS OTP test sent via %s (id=%s recipient=%s)",
            provider,
            self.id,
            self.recipient,
        )
        return result

    def send_sms_otp(self):
        primary = self._pick_weighted_provider()
        if not primary:
            self.make_as_fail("No SMS OTP provider with available credit.")
            return False

        last_reason = False
        tried = set()

        def try_provider(provider):
            if not provider or provider in tried:
                return None
            tried.add(provider)
            self.write({"sms_provider": provider})
            return self._attempt_send_sms_provider(provider)

        for provider in (primary, self._other_sms_provider(primary)):
            result, detail = try_provider(provider) or (None, None)
            if detail is None and result is None:
                continue
            if result:
                log_reason = f"[{provider}] {detail}"
                self.make_as_send(log_reason)
                _logger.info(
                    "crm.otp SMS OTP sent via %s (id=%s recipient=%s)",
                    provider,
                    self.id,
                    self.recipient,
                )
                return result

            last_reason = detail
            if not self._is_out_of_credit_failure(detail):
                break

        self.make_as_fail(last_reason or "failed to send sms otp.")
        return False

    def verify_sms_otp(self, otp_code):
        provider = self.sms_provider or GODYSMS_PROVIDER

        if provider == THAIBULKSMS_PROVIDER:
            if not self._thaibulksms_otp_configured():
                self.make_as_fail("THAIBULKSMS_OTP_KEY/SECRET not configured.")
                return False
            verified = self._verify_thaibulksms_otp(otp_code)
        else:
            if not os.getenv("SMS_API_TOKEN", False):
                self.make_as_fail("SMS_API_TOKEN not configured.")
                return False
            verified = self._verify_godysms_otp(otp_code)

        _logger.info(
            "crm.otp SMS OTP verify via %s (id=%s verified=%s)",
            provider,
            self.id,
            bool(verified),
        )
        return verified
