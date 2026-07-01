from odoo import fields, models


class PartnerPortalApiKeyWizard(models.TransientModel):
    _name = "partner.portal.api.key.wizard"
    _description = "Portal API Key"

    user_id = fields.Many2one(
        "res.users",
        string="Portal User",
        required=True,
        readonly=True,
    )
    api_key = fields.Char(string="API Key", readonly=True)
