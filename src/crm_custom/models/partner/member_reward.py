from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerMemberReward(models.Model):
    _name = "partner.member.reward"
    _description = "Partner Member Reward"
    _order = "sequence, id"

    sequence = fields.Integer(string="Sequence", default=10)
    name = fields.Char(string="Name", compute="_compute_name", store=True)

    event = fields.Selection(
        [
            ("join", "New Member"),
            ("tier_promotion", "Tier Promotion"),
        ],
        string="Event",
        required=True,
    )
    reward_type = fields.Selection(
        [
            ("point", "Point"),
            ("coupon", "Coupon"),
        ],
        string="Reward Type",
        required=True,
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    tier_id = fields.Many2one(
        "partner.tier",
        string="Tier",
        ondelete="cascade",
        domain="[('partner_id', '=', partner_id)]",
    )

    point_value = fields.Float(string="Point Value")
    point_currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Point Currency",
        domain="[('partner_id', '=', partner_id)]",
    )
    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        domain="[('partner_id', '=', partner_id)]",
    )

    @api.depends(
        "event",
        "reward_type",
        "point_value",
        "point_currency_id",
        "point_currency_id.name",
        "coupon_id",
        "coupon_id.name",
        "tier_id",
        "tier_id.name",
    )
    def _compute_name(self):
        for record in self:
            if record.reward_type == "point":
                currency_name = record.point_currency_id.name or "point"
                reward_label = f"{record.point_value:g} {currency_name}"
            else:
                reward_label = record.coupon_id.name or "Coupon"

            if record.event == "join":
                record.name = f"Join: {reward_label}"
            else:
                tier_name = record.tier_id.name or "Tier"
                record.name = f"{tier_name} promotion: {reward_label}"

    @api.model_create_multi
    def create(self, vals_list):
        prepared_vals_list = []
        for vals in vals_list:
            prepared_vals = dict(vals)
            if prepared_vals.get("tier_id"):
                tier = self.env["partner.tier"].browse(prepared_vals["tier_id"])
                prepared_vals["partner_id"] = tier.partner_id.id
                prepared_vals["event"] = "tier_promotion"
            elif prepared_vals.get("event") != "tier_promotion":
                prepared_vals["event"] = prepared_vals.get("event", "join")
            prepared_vals_list.append(prepared_vals)
        return super().create(prepared_vals_list)

    @api.constrains("event", "tier_id")
    def _check_event_tier(self):
        for record in self:
            if record.event == "join" and record.tier_id:
                raise ValidationError("รางวัลสมาชิกใหม่ไม่ต้องระบุ Tier")
            if record.event == "tier_promotion" and not record.tier_id:
                raise ValidationError("รางวัลเลื่อน Tier ต้องระบุ Tier")

    @api.constrains("reward_type", "point_value", "point_currency_id", "coupon_id")
    def _check_reward_configuration(self):
        for record in self:
            if record.reward_type == "point":
                if record.point_value <= 0:
                    raise ValidationError("Point value ต้องมากกว่า 0")
                if not record.point_currency_id:
                    raise ValidationError("กรุณาเลือก Point currency")
            elif record.reward_type == "coupon" and not record.coupon_id:
                raise ValidationError("กรุณาเลือก Coupon")

    @api.constrains("partner_id", "point_currency_id", "coupon_id", "tier_id")
    def _check_partner_consistency(self):
        for record in self:
            if (
                record.point_currency_id
                and record.point_currency_id.partner_id != record.partner_id
            ):
                raise ValidationError("Point currency ต้องอยู่ใน Partner เดียวกัน")
            if record.coupon_id and record.coupon_id.partner_id != record.partner_id:
                raise ValidationError("Coupon ต้องอยู่ใน Partner เดียวกัน")
            if record.tier_id and record.tier_id.partner_id != record.partner_id:
                raise ValidationError("Tier ต้องอยู่ใน Partner เดียวกัน")
