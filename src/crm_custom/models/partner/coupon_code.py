from odoo import fields, models


class PartnerCouponCode(models.Model):
    _name = "partner.coupon.code"
    _description = "Partner Coupon Code"
    _order = "code"
    _sql_constraints = [
        ("coupon_code_uniq", "unique(code)", "Coupon code must be unique."),
    ]

    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        required=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    code = fields.Char(string="Code", required=True, readonly=True, copy=False)
    state = fields.Selection(
        [
            ("available", "Available"),
            ("redeemed", "Redeemed"),
            ("used", "Used"),
        ],
        string="Status",
        default="available",
        readonly=True,
        required=True,
    )
    user_coupon_id = fields.Many2one(
        "crm.user.coupon",
        string="User Coupon",
        readonly=True,
        ondelete="set null",
    )
    redeemed_by_user_id = fields.Many2one(
        "crm.user",
        string="Redeemed By",
        readonly=True,
        ondelete="set null",
    )
    redeemed_date = fields.Datetime(string="Redeemed Date", readonly=True)
    used_by_user_id = fields.Many2one(
        "crm.user",
        string="Used By",
        readonly=True,
        ondelete="set null",
    )
    used_date = fields.Datetime(string="Used Date", readonly=True)
