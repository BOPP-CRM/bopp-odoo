from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UserPointRedeemGenerateWizard(models.TransientModel):
    _name = "crm.user.point.redeem.generate.wizard"
    _description = "Generate Point Redeem QR"

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        readonly=True,
    )
    name = fields.Char(string="Name", required=True)
    value = fields.Float(string="Value", required=True, default=1)
    type = fields.Selection([
        ("earn", "Earn"),
        ("transfer", "Transfer"),
        ("burn", "Burn"),
    ], string="Type", required=True, default="earn")
    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
    )
    limit_per_user = fields.Integer(string="Limit per user", default=1)
    limit_per_qr = fields.Integer(string="Limit per QR", default=1)
    expiration_date = fields.Datetime(string="Expiration Date")
    reward_coupon_id = fields.Many2one(
        "partner.coupon",
        string="Reward Coupon",
        domain="[('partner_id', '=', partner_id)]",
    )

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

    def action_generate(self):
        self.ensure_one()
        if self.currency_id.partner_id != self.partner_id:
            raise ValidationError("Point currency must belong to this partner.")

        redeem = self.env["crm.partner.point.redeem"].create({
            "partner_id": self.partner_id.id,
            "name": self.name,
            "value": self.value,
            "type": self.type,
            "currency_id": self.currency_id.id,
            "limit_per_user": self.limit_per_user,
            "limit_per_qr": self.limit_per_qr,
            "expiration_date": self.expiration_date,
            "reward_coupon_id": self.reward_coupon_id.id if self.reward_coupon_id else False,
        })

        return {
            "type": "ir.actions.act_window",
            "name": "Redeem QR",
            "res_model": "crm.partner.point.redeem",
            "res_id": redeem.id,
            "view_mode": "form",
            "target": "current",
        }
