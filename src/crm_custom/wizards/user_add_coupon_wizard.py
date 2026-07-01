from odoo import fields, models
from odoo.exceptions import ValidationError


class UserAddCouponWizard(models.TransientModel):
    _name = "crm.user.add.coupon.wizard"
    _description = "Add Coupon To User"

    user_id = fields.Many2one(
        "crm.user",
        string="User",
        required=True,
        readonly=True,
    )
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        readonly=True,
    )
    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        required=True,
        domain="[('partner_id', '=', partner_id)]",
    )
    note = fields.Text(string="Note", required=True)

    def action_add_coupon(self):
        self.ensure_one()
        if not self.note or not self.note.strip():
            raise ValidationError("กรุณาระบุหมายเหตุ")
        if self.coupon_id.partner_id != self.partner_id:
            raise ValidationError("Coupon must belong to this partner.")

        self.coupon_id.grant_to_user(self.user_id, self.note.strip())
        return {"type": "ir.actions.act_window_close"}
