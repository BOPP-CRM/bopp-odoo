from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UserPoint(models.Model):
    _name = "crm.user.point"
    _description = "Point"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True)
    admin_note = fields.Text(string="Admin Note", tracking=True)
    value = fields.Float(string="Value", tracking=True, required=True)
    type = fields.Selection([
        ("earn", "Earn"),
        ("transfer", "Transfer"),
        ("burn", "Burn"),
    ], string="Type", required=True)
    given_date = fields.Datetime(string="Given Date", tracking=True, required=True)
    expiration_date = fields.Datetime(string="Expiration Date", tracking=True)

    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
        ondelete="cascade",
    )

    user_id = fields.Many2one(
        "crm.user",
        string="User",
        required=True,
        ondelete="cascade",
    )

    point_redeem_id = fields.Many2one(
        "crm.partner.point.redeem",
        string="Redeem",
    )

    receipt_redeem_id = fields.Many2one(
        "crm.partner.receipt.redeem",
        string="Receipt Redeem",
    )

    member_reward_id = fields.Many2one(
        "partner.member.reward",
        string="Member Reward",
        readonly=True,
        ondelete="set null",
    )
    member_reward_event = fields.Selection(
        [
            ("join", "Join"),
            ("tier_promotion", "Tier Promotion"),
        ],
        string="Member Reward Event",
        readonly=True,
    )
    member_reward_tier_id = fields.Many2one(
        "partner.tier",
        string="Member Reward Tier",
        readonly=True,
        ondelete="set null",
    )

    @api.constrains("currency_id", "user_id")
    def _check_currency_partner(self):
        for record in self:
            if (
                record.currency_id
                and record.user_id
                and record.currency_id.partner_id != record.user_id.partner_id
            ):
                raise ValidationError("Point currency must belong to the user's partner.")

    @api.model_create_multi
    def create(self, vals_list):
        points = super().create(vals_list)
        points._trigger_tier_update()
        return points

    def write(self, vals):
        result = super().write(vals)
        if any(field in vals for field in ("value", "type", "currency_id", "user_id")):
            self._trigger_tier_update()
        return result

    def unlink(self):
        users = self.mapped("user_id")
        result = super().unlink()
        users._update_tier()
        return result

    def _trigger_tier_update(self):
        spending_points = self.filtered(lambda point: point.currency_id.is_total_spending)
        if spending_points:
            spending_points.mapped("user_id")._update_tier()
