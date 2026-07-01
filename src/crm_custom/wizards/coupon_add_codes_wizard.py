from odoo import api, fields, models

from ..models.partner.coupon import MAX_CODE_BATCH_SIZE


class PartnerCouponAddCodesWizard(models.TransientModel):
    _name = "partner.coupon.add.codes.wizard"
    _description = "Add Partner Coupon Codes"

    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        required=True,
        readonly=True,
    )
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        readonly=True,
    )
    add_source = fields.Selection(
        [
            ("generate", "Generate"),
            ("import", "Import CSV"),
        ],
        string="Add Method",
        required=True,
        default="generate",
    )
    code_quantity = fields.Integer(
        string="Quantity",
        default=1,
        help=f"สูงสุด {MAX_CODE_BATCH_SIZE} รายการต่อครั้ง",
    )
    prefix_code = fields.Char(string="Prefix Code")
    random_range = fields.Integer(string="Random Range")
    suffix_code = fields.Char(string="Suffix Code")
    import_file = fields.Binary(string="CSV File")
    import_filename = fields.Char(string="CSV Filename")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        coupon_id = values.get("coupon_id") or self.env.context.get("default_coupon_id")
        if coupon_id:
            coupon = self.env["partner.coupon"].browse(coupon_id)
            values.setdefault("prefix_code", coupon.prefix_code)
            values.setdefault("suffix_code", coupon.suffix_code)
            values.setdefault(
                "random_range",
                max(coupon.locked_random_range or 0, coupon.random_range or 6),
            )
        return values

    def action_add_codes(self):
        self.ensure_one()
        coupon = self.coupon_id

        coupon.add_codes(
            add_source=self.add_source,
            code_quantity=self.code_quantity,
            prefix_code=self.prefix_code,
            random_range=self.random_range,
            suffix_code=self.suffix_code,
            import_file=self.import_file,
            import_filename=self.import_filename,
        )

        return {
            "type": "ir.actions.act_window",
            "name": "Coupon",
            "res_model": "partner.coupon",
            "res_id": coupon.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_download_import_template(self):
        return self.env["partner.coupon"].action_download_import_template()
