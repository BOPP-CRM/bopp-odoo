from odoo import api, fields, models

DEFAULT_OTHER_CONTRIBUTOR_NAME = "อื่น ๆ"


class PartnerWarrantyContributor(models.Model):
    _name = "partner.warranty.contributor"
    _description = "Partner Warranty Purchase Channel"
    _order = "sequence asc, name asc, id asc"

    name = fields.Char(string="Name", required=True)
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(string="Active", default=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    _sql_constraints = [
        (
            "partner_warranty_contributor_name_uniq",
            "unique(partner_id, name)",
            "Purchase channel name must be unique per partner.",
        ),
    ]

    @api.model
    def ensure_default_items(self, partner):
        existing = self.with_context(active_test=False).search([
            ("partner_id", "=", partner.id),
            ("name", "=", DEFAULT_OTHER_CONTRIBUTOR_NAME),
        ], limit=1)
        if existing:
            # if not existing.active:
            #     existing.write({"active": True})
            return existing

        return self.create({
            "name": DEFAULT_OTHER_CONTRIBUTOR_NAME,
            "sequence": 999,
            "partner_id": partner.id,
        })
