from odoo import api, fields, models
from odoo.exceptions import ValidationError

DEFAULT_OTHER_PRODUCT_NAME = "อื่น ๆ"


class PartnerWarrantyProduct(models.Model):
    _name = "partner.warranty.product"
    _description = "Partner Warranty Product"
    _inherit = ["s3.image.mixin"]
    _order = "name asc, id asc"

    name = fields.Char(string="Name", required=True)
    description = fields.Text(string="Description")
    sku = fields.Char(string="SKU")
    cost_price = fields.Float(string="Cost Price")
    sell_price = fields.Float(string="Sell Price")
    image = fields.Char(string="Image")
    image_file = fields.Image(
        string="Image",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_image_file",
        inverse="_inverse_image_file",
    )
    active = fields.Boolean(string="Active", default=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    _sql_constraints = [
        (
            "partner_warranty_product_name_uniq",
            "unique(partner_id, name)",
            "Product name must be unique per partner.",
        ),
    ]

    def _get_s3_image_config(self):
        return {
            "image": {
                "max_width": 1920,
                "max_height": 1920,
            },
        }

    @api.depends("image")
    def _compute_image_file(self):
        self._compute_s3_image_file("image")

    def _inverse_image_file(self):
        self._inverse_s3_image_file("image")

    @api.constrains("cost_price", "sell_price")
    def _check_prices(self):
        for record in self:
            if record.cost_price < 0 or record.sell_price < 0:
                raise ValidationError("ราคาต้องไม่ติดลบ")

    @api.model
    def ensure_default_items(self, partner):
        existing = self.with_context(active_test=False).search([
            ("partner_id", "=", partner.id),
            ("name", "=", DEFAULT_OTHER_PRODUCT_NAME),
        ], limit=1)
        if existing:
            # if not existing.active:
            #     existing.write({"active": True})
            return existing

        return self.create({
            "name": DEFAULT_OTHER_PRODUCT_NAME,
            "partner_id": partner.id,
        })
