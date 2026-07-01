from odoo import models, fields


class PartnerCustomField(models.Model):
    _name = "partner.custom.field"
    _description = "Partner Custom Field"
    _order = "create_date desc"

    key = fields.Char(string="Key", required=True)
    value = fields.Char(string="Value", required=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
