from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerTier(models.Model):
    _name = "partner.tier"
    _description = "Partner Tier"
    _inherit = ["s3.image.mixin"]
    _order = "min_spending asc, id asc"
    _sql_constraints = [
        (
            "partner_tier_code_uniq",
            "unique(code, partner_id)",
            "Tier code must be unique per partner.",
        ),
    ]

    name = fields.Char(string="Name", required=True)
    code = fields.Char(string="Code", required=True)
    color = fields.Char(string="Color")
    icon = fields.Char(string="Logo")
    icon_file = fields.Image(
        string="Logo",
        max_width=500,
        max_height=500,
        store=False,
        compute="_compute_icon_file",
        inverse="_inverse_icon_file",
    )

    convert_points = fields.Float(string="Convert Points", required=True, default=25)
    min_spending = fields.Float(string="Minimum Spending", required=True)
    max_spending = fields.Float(string="Maximum Spending", required=True)
    is_show_in_ui = fields.Boolean(string="Show In UI", default=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    promotion_reward_ids = fields.One2many(
        "partner.member.reward",
        "tier_id",
        string="Promotion Rewards",
        domain=[("event", "=", "tier_promotion")],
    )

    def _get_s3_image_config(self):
        return {
            "icon": {
                "max_width": 500,
                "max_height": 500,
            },
        }

    @api.depends("icon")
    def _compute_icon_file(self):
        self._compute_s3_image_file("icon")

    def _inverse_icon_file(self):
        self._inverse_s3_image_file("icon")

    @api.constrains("min_spending", "max_spending")
    def _check_spending_range(self):
        for record in self:
            if record.min_spending > record.max_spending:
                raise ValidationError(
                    "Minimum Spending ต้องไม่มากกว่า Maximum Spending"
                )

    @api.constrains("min_spending", "partner_id")
    def _check_partner_has_base_tier(self):
        for record in self:
            if not record.partner_id:
                continue

            has_base_tier = self.search_count([
                ("partner_id", "=", record.partner_id.id),
                ("min_spending", "=", 0),
            ])
            if not has_base_tier:
                raise ValidationError(
                    "Partner ต้องมี Tier เริ่มต้นที่มี Minimum Spending = 0 เสมอ"
                )

    @api.constrains("min_spending", "max_spending", "partner_id")
    def _check_spending_overlap(self):
        for record in self:
            if not record.partner_id:
                continue

            other_tiers = self.search([
                ("partner_id", "=", record.partner_id.id),
                ("id", "!=", record.id),
            ])
            for other in other_tiers:
                if (
                    record.min_spending <= other.max_spending
                    and other.min_spending <= record.max_spending
                ):
                    raise ValidationError(
                        f"ช่วง spending ของ Tier '{record.name}' "
                        f"({record.min_spending:g} - {record.max_spending:g}) "
                        f"ซ้อนทับกับ Tier '{other.name}' "
                        f"({other.min_spending:g} - {other.max_spending:g})"
                    )

    @api.model_create_multi
    def create(self, vals_list):
        tiers = super().create(vals_list)
        tiers.mapped("partner_id").mapped("user_ids")._update_tier()
        return tiers

    def write(self, vals):
        partners = self.mapped("partner_id")
        result = super().write(vals)
        if any(field in vals for field in ("min_spending", "max_spending", "partner_id")):
            partners.mapped("user_ids")._update_tier()
        return result

    def unlink(self):
        for record in self:
            if record.min_spending == 0:
                other_base_tier_count = self.search_count([
                    ("partner_id", "=", record.partner_id.id),
                    ("min_spending", "=", 0),
                    ("id", "!=", record.id),
                ])
                if other_base_tier_count == 0:
                    raise ValidationError(
                        "Partner ต้องมี Tier เริ่มต้นที่มี Minimum Spending = 0 เสมอ"
                    )

        partners = self.mapped("partner_id")
        result = super().unlink()
        partners.mapped("user_ids")._update_tier()
        return result
