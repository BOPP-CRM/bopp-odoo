from odoo import fields, models
from odoo.exceptions import ValidationError


class PartnerPortalUserWizard(models.TransientModel):
    _name = "partner.portal.user.wizard"
    _description = "Create Partner Portal User"

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        readonly=True,
    )
    name = fields.Char(string="Name", required=True)
    email = fields.Char(string="Email", required=True)
    password = fields.Char(string="Password", required=True)
    portal_role = fields.Selection(
        selection=[
            ("admin", "Admin"),
            ("operation", "Operation"),
        ],
        string="Portal Role",
        default="admin",
        required=True,
    )

    def action_create(self):
        self.ensure_one()
        if not self.password or len(self.password) < 8:
            raise ValidationError("Password must be at least 8 characters.")

        self.env["res.users"].create_partner_portal_user(
            self.partner_id,
            self.name.strip(),
            self.email.strip(),
            self.password,
            portal_role=self.portal_role,
        )
        return {"type": "ir.actions.act_window_close"}
