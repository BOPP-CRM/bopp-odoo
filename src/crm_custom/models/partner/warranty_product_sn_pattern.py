import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError


def pattern_to_regex(pattern):
    regex_parts = []
    for char in pattern:
        if char == "#":
            regex_parts.append(r"\d")
        elif char == "?":
            regex_parts.append(r".")
        elif char == "*":
            regex_parts.append(r".*")
        else:
            regex_parts.append(re.escape(char))
    return "^" + "".join(regex_parts) + "$"


def serial_matches_pattern(serial_number, pattern):
    if not pattern:
        return False
    try:
        return bool(re.match(pattern_to_regex(pattern), serial_number or ""))
    except re.error:
        return False


class PartnerWarrantyProductSnPattern(models.Model):
    _name = "partner.warranty.product.sn.pattern"
    _description = "Partner Warranty Product Serial Number Pattern"
    _order = "sequence asc, id asc"

    product_id = fields.Many2one(
        "partner.warranty.product",
        string="Product",
        required=True,
        ondelete="cascade",
    )
    pattern = fields.Char(string="Pattern", required=True)
    sequence = fields.Integer(string="Sequence", default=10)

    _sql_constraints = [
        (
            "partner_warranty_product_sn_pattern_uniq",
            "unique(product_id, pattern)",
            "Pattern must be unique per product.",
        ),
    ]

    @api.constrains("pattern")
    def _check_pattern(self):
        for record in self:
            pattern = (record.pattern or "").strip()
            if not pattern:
                raise ValidationError("กรุณาระบุรูปแบบ Serial Number")
            try:
                re.compile(pattern_to_regex(pattern))
            except re.error as error:
                raise ValidationError(
                    f"รูปแบบ Serial Number ไม่ถูกต้อง: {error}"
                ) from error
