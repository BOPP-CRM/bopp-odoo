from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ...util.phone import is_valid_th_msisdn, normalize_th_msisdn

from ..models.system.sms_otp_providers import GODYSMS_PROVIDER, THAIBULKSMS_PROVIDER


class OtpSmsTestWizard(models.TransientModel):
    _name = "crm.otp.sms.test.wizard"
    _description = "SMS OTP Test"

    phone = fields.Char(string="Phone (MSISDN)", required=True)
    partner_id = fields.Many2one("partner", string="Partner", required=True)
    sms_provider = fields.Selection(
        selection=[
            (GODYSMS_PROVIDER, "Gody SMS"),
            (THAIBULKSMS_PROVIDER, "ThaiBulkSMS"),
        ],
        string="SMS Provider",
        required=True,
        default=GODYSMS_PROVIDER,
    )

    otp_id = fields.Many2one("crm.otp", string="OTP record", readonly=True)
    send_success = fields.Boolean(string="Send succeeded", readonly=True)
    send_detail = fields.Text(string="Send status", readonly=True)

    otp_pin = fields.Char(string="OTP code from SMS")
    verify_success = fields.Boolean(string="Verify succeeded", readonly=True)
    verify_detail = fields.Char(string="Verify status", readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "partner_id" in fields_list and not values.get("partner_id"):
            partner = self.env["partner"].search([], limit=1)
            if partner:
                values["partner_id"] = partner.id
        return values

    def _reload_form(self):
        return {
            "type": "ir.actions.act_window",
            "name": "SMS OTP Test",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_send_test_sms(self):
        self.ensure_one()
        phone = (self.phone or "").strip()
        if not phone:
            raise ValidationError("Enter a phone number.")

        msisdn = normalize_th_msisdn(phone)
        if not is_valid_th_msisdn(msisdn):
            raise ValidationError(
                "Invalid Thai mobile number. Examples: 0646614513, 646614513, or 66646614513."
            )

        otp = self.env["crm.otp"].sudo().create({
            "recipient": msisdn,
            "type": "phone",
            "key": "sms_test",
            "key_id": str(self.env.user.id),
            "partner_id": self.partner_id.id,
            "expiry": fields.Datetime.now() + timedelta(minutes=15),
        })
        otp.send_sms_otp_via_provider(self.sms_provider)

        self.write({
            "otp_id": otp.id,
            "send_success": otp.is_sent,
            "send_detail": otp.reason or ("Sent." if otp.is_sent else "Failed."),
            "otp_pin": False,
            "verify_success": False,
            "verify_detail": False,
        })
        return self._reload_form()

    def action_verify_otp(self):
        self.ensure_one()
        if not self.otp_id:
            raise ValidationError("Send a test SMS first.")
        if not self.send_success:
            raise ValidationError("Last send failed; fix send before verifying.")
        pin = (self.otp_pin or "").strip()
        if not pin:
            raise ValidationError("Enter the OTP code received by SMS.")

        verified = self.otp_id.verify_sms_otp(pin)
        self.write({
            "verify_success": bool(verified),
            "verify_detail": (
                "OTP verified successfully."
                if verified
                else "OTP verification failed (wrong code or expired)."
            ),
        })
        return self._reload_form()
