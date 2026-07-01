from odoo import fields, models


class PartnerWarrantyComment(models.Model):
    _name = "partner.warranty.comment"
    _description = "Partner Warranty Comment"
    _order = "create_date asc, id asc"

    body = fields.Text(string="Comment", required=True)
    author_id = fields.Many2one(
        "res.users",
        string="Author",
        ondelete="set null",
    )
    author_name = fields.Char(string="Author Name")

    warranty_id = fields.Many2one(
        "partner.warranty",
        string="Warranty",
        required=True,
        ondelete="cascade",
    )
