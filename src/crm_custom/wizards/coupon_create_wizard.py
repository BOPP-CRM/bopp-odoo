from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..models.partner.coupon import MAX_CODE_BATCH_SIZE


class PartnerCouponCreateWizard(models.TransientModel):
    _name = "partner.coupon.create.wizard"
    _description = "Create Partner Coupon"
    _inherit = ["s3.image.mixin"]

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        readonly=True,
    )
    name = fields.Char(string="Name", required=True)
    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
    )
    value = fields.Float(string="Value", required=True, default=0)
    image = fields.Char(string="Image")
    image_file = fields.Image(
        string="Image",
        max_width=1000,
        max_height=1000,
        store=False,
    )
    term_and_condition = fields.Text(string="Term and Condition")
    start_time = fields.Datetime(
        string="Start Date",
        required=True,
        default=fields.Datetime.now,
    )
    code_expiry_interval = fields.Integer(
        string="Expiry Interval (Minutes)",
        default=15,
    )
    end_time = fields.Datetime(string="End Date")
    is_show_in_ui = fields.Boolean(string="Show In UI", default=True)
    max_redeem_per_user = fields.Integer(
        string="Max Redeem Per User",
        default=0,
        help="0 = ไม่จำกัด",
    )
    code_source = fields.Selection(
        [
            ("generate", "Generate"),
            ("import", "Import CSV"),
        ],
        string="Code Source",
        required=True,
        default="generate",
    )
    code_quantity = fields.Integer(
        string="Quantity",
        default=1,
        help=f"สูงสุด {MAX_CODE_BATCH_SIZE} รายการต่อครั้ง",
    )
    prefix_code = fields.Char(string="Prefix Code")
    random_range = fields.Integer(string="Random Range", default=6)
    suffix_code = fields.Char(string="Suffix Code")
    import_file = fields.Binary(string="CSV File")
    import_filename = fields.Char(string="CSV Filename")

    def _get_s3_image_config(self):
        return {
            "image": {
                "max_width": 1000,
                "max_height": 1000,
            },
        }

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        partner_id = values.get("partner_id") or self.env.context.get("default_partner_id")
        if partner_id and "currency_id" in fields_list:
            partner = self.env["partner"].browse(partner_id)
            currency = partner.currency_ids.filtered("is_default")[:1] or partner.currency_ids[:1]
            if currency:
                values["currency_id"] = currency.id

        return values

    def action_create(self):
        self.ensure_one()
        if self.currency_id.partner_id != self.partner_id:
            raise ValidationError("Coupon currency must belong to this partner.")

        if self.code_source == "generate":
            self.env["partner.coupon"]._validate_code_batch_size(self.code_quantity)
            if self.random_range <= 0:
                raise ValidationError("Random range ต้องมากกว่า 0")
        elif not self.import_file:
            raise ValidationError("กรุณาอัปโหลดไฟล์ CSV")

        coupon = self.env["partner.coupon"].create({
            "partner_id": self.partner_id.id,
            "name": self.name,
            "currency_id": self.currency_id.id,
            "value": self.value,
            "image": self._upload_image_field("image", self.image_file),
            "term_and_condition": self.term_and_condition,
            "start_time": self.start_time,
            "code_expiry_interval": self.code_expiry_interval,
            "end_time": self.end_time,
            "is_show_in_ui": self.is_show_in_ui,
            "max_redeem_per_user": self.max_redeem_per_user,
            "code_source": self.code_source,
            "prefix_code": self.prefix_code,
            "random_range": self.random_range,
            "suffix_code": self.suffix_code,
            "locked_random_range": self.random_range if self.code_source == "generate" else 0,
        })

        if self.code_source == "generate":
            coupon.create_generated_codes(self.code_quantity, self.random_range)
        else:
            coupon.import_codes_from_file(self.import_file, self.import_filename)

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
