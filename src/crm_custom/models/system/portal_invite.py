import os
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerPortalInvite(models.Model):
    _name = "partner.portal.invite"
    _description = "Partner Portal Invite"
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True)
    email = fields.Char(string="Email", required=True, index=True)
    token = fields.Char(
        string="Token",
        required=True,
        copy=False,
        default=lambda self: secrets.token_urlsafe(32),
        index=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("accepted", "Accepted"),
            ("cancelled", "Cancelled"),
            ("expired", "Expired"),
        ],
        string="Status",
        default="pending",
        required=True,
        index=True,
    )
    expires_at = fields.Datetime(string="Expires At", required=True, index=True)
    accepted_at = fields.Datetime(string="Accepted At", readonly=True)
    invite_url = fields.Char(string="Invite URL", compute="_compute_invite_url")
    portal_role = fields.Selection(
        selection=[
            ("admin", "Admin"),
            ("operation", "Operation"),
        ],
        string="Portal Role",
        default="admin",
        required=True,
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    invited_by_id = fields.Many2one(
        "res.users",
        string="Invited By",
        required=True,
        ondelete="restrict",
    )
    accepted_user_id = fields.Many2one(
        "res.users",
        string="Accepted User",
        readonly=True,
        ondelete="set null",
    )

    @api.model
    def _get_invite_lifetime_hours(self):
        return int(os.getenv("PORTAL_INVITE_EXPIRE_HOURS", "72"))

    @api.depends("token", "partner_id.slug")
    def _compute_invite_url(self):
        frontend_path = (os.getenv("PORTAL_FRONTEND_PATH") or "").rstrip("/")
        for record in self:
            if not record.token or not record.partner_id.slug:
                record.invite_url = False
                continue

            if frontend_path:
                record.invite_url = (
                    f"{frontend_path}/{record.partner_id.slug}/portal/invite"
                    f"?token={record.token}"
                )
                continue

            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
            record.invite_url = (
                f"{base_url}/api/portal/invite/{record.token}"
            )

    @api.model
    def create_invite(self, partner, invited_by, name, email, portal_role=None):
        normalized_email = self.env["res.users"]._normalize_portal_email(email)
        if not normalized_email:
            raise ValidationError("Email is required.")
        portal_role = self.env["res.users"]._validate_portal_role(
            portal_role or self.env["res.users"].PORTAL_ROLE_ADMIN
        )

        if self.env["res.users"]._find_portal_user(partner, normalized_email, active_only=True):
            raise ValidationError(
                f"Email '{normalized_email}' is already used for this partner."
            )

        pending_invite = self.search([
            ("partner_id", "=", partner.id),
            ("email", "=", normalized_email),
            ("state", "=", "pending"),
            ("expires_at", ">=", fields.Datetime.now()),
        ], limit=1)
        if pending_invite:
            raise ValidationError(
                f"Email '{normalized_email}' already has a pending invite."
            )

        now = fields.Datetime.now()
        return self.create({
            "name": (name or normalized_email).strip(),
            "email": normalized_email,
            "partner_id": partner.id,
            "invited_by_id": invited_by.id,
            "portal_role": portal_role,
            "expires_at": now + timedelta(hours=self._get_invite_lifetime_hours()),
        })

    @api.model
    def _get_valid_invite(self, token):
        if not token:
            return self.browse()

        invite = self.sudo().search([("token", "=", token)], limit=1)
        if not invite:
            return self.browse()

        if invite.state == "pending" and invite.expires_at < fields.Datetime.now():
            invite.write({"state": "expired"})
            return self.browse()

        if invite.state != "pending":
            return self.browse()

        return invite

    def accept(self, password, name=None):
        self.ensure_one()
        invite = self._get_valid_invite(self.token)
        if not invite:
            raise ValidationError("Invite link is invalid or expired.")

        if len(password or "") < 8:
            raise ValidationError("Password must be at least 8 characters.")

        user = self.env["res.users"].create_partner_portal_user(
            invite.partner_id,
            (name or invite.name).strip(),
            invite.email,
            password,
            portal_role=invite.portal_role,
        )
        invite.write({
            "state": "accepted",
            "accepted_at": fields.Datetime.now(),
            "accepted_user_id": user.id,
        })
        return user

    def action_cancel(self):
        for invite in self:
            if invite.state != "pending":
                raise ValidationError("Only pending invites can be cancelled.")
            invite.write({"state": "cancelled"})
