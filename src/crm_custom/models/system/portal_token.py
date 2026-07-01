import os
import secrets
from datetime import timedelta

from odoo import api, fields, models


class PartnerPortalToken(models.Model):
    _name = "partner.portal.token"
    _description = "Partner Portal Token"
    _order = "create_date desc"

    name = fields.Char(string="Label", required=True)
    token = fields.Char(
        string="Token",
        required=True,
        copy=False,
        default=lambda self: secrets.token_urlsafe(32),
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    expires_at = fields.Datetime(string="Expires At", required=True, index=True)
    active = fields.Boolean(default=True)

    @api.model
    def _get_token_lifetime_hours(self):
        return int(os.getenv("PORTAL_TOKEN_EXPIRE_HOURS", "168"))

    @api.model
    def create_for_user(self, user):
        now = fields.Datetime.now()
        expires_at = now + timedelta(hours=self._get_token_lifetime_hours())
        self.search([
            ("user_id", "=", user.id),
            ("active", "=", True),
        ]).write({"active": False})

        return self.create({
            "name": f"{user.login} {fields.Datetime.to_string(now)}",
            "user_id": user.id,
            "partner_id": user.crm_partner_id.id,
            "expires_at": expires_at,
        })

    @api.model
    def revoke_for_user(self, user):
        self.search([
            ("user_id", "=", user.id),
            ("active", "=", True),
        ]).write({"active": False})

    @api.model
    def get_user_from_token(self, token):
        if not token:
            return self.env["res.users"]

        now = fields.Datetime.now()
        portal_token = self.search([
            ("token", "=", token),
            ("active", "=", True),
            ("expires_at", ">=", now),
        ], limit=1)
        if not portal_token:
            return self.env["res.users"]

        user = portal_token.user_id
        if (
            not user.active
            or not user.is_partner_portal
            or not user.crm_partner_id
            or portal_token.partner_id != user.crm_partner_id
        ):
            return self.env["res.users"]

        return user
