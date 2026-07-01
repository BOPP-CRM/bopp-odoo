from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class UserCoupon(models.Model):
    _name = "crm.user.coupon"
    _description = "User Coupon"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _sql_constraints = [
        ("user_coupon_code_uniq", "unique(code)", "Coupon code must be unique."),
    ]

    name = fields.Char(string="Name", required=True, tracking=True)
    admin_note = fields.Text(string="Admin Note", tracking=True)
    code = fields.Char(string="Code", required=True, readonly=True, copy=False, tracking=True)
    value = fields.Float(string="Value", required=True, readonly=True, tracking=True)
    acquired_date = fields.Datetime(string="Acquired Date", required=True, readonly=True, tracking=True)
    activated_date = fields.Datetime(string="Activated Date", readonly=True, tracking=True)
    expiration_date = fields.Datetime(string="Expiration Date", readonly=True, tracking=True)
    state = fields.Selection(
        [
            ("redeemed", "Redeemed"),
            ("activated", "Activated"),
            ("expired", "Expired"),
            ("used", "Used"),
        ],
        string="State",
        default="redeemed",
        required=True,
        tracking=True,
    )
    is_used = fields.Boolean(string="Used", default=False, tracking=True)
    used_date = fields.Datetime(string="Used Date", readonly=True, tracking=True)

    user_id = fields.Many2one(
        "crm.user",
        string="User",
        required=True,
        ondelete="cascade",
    )
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Partner Coupon",
        required=True,
        ondelete="restrict",
    )
    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
        ondelete="restrict",
    )
    point_id = fields.Many2one(
        "crm.user.point",
        string="Point Transaction",
        readonly=True,
        ondelete="restrict",
    )
    coupon_code_id = fields.Many2one(
        "partner.coupon.code",
        string="Coupon Code",
        readonly=True,
        ondelete="set null",
    )
    point_redeem_id = fields.Many2one(
        "crm.partner.point.redeem",
        string="Point Redeem",
        readonly=True,
        ondelete="set null",
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

    def init(self):
        super().init()
        self._cr.execute(
            """
            UPDATE crm_user_coupon
               SET state = 'used'
             WHERE is_used IS TRUE
               AND state = 'redeemed'
            """
        )
        self._cr.execute(
            """
            UPDATE crm_user_coupon uc
               SET expiration_date = pc.end_time
              FROM partner_coupon pc
             WHERE uc.coupon_id = pc.id
               AND uc.state = 'redeemed'
               AND uc.expiration_date IS NULL
               AND pc.end_time IS NOT NULL
            """
        )
        self._cr.execute(
            """
            UPDATE crm_user_coupon uc
               SET state = 'expired'
              FROM partner_coupon pc
             WHERE uc.coupon_id = pc.id
               AND uc.is_used IS NOT TRUE
               AND uc.state IN ('redeemed', 'activated')
               AND (
                   (uc.expiration_date IS NOT NULL AND uc.expiration_date < NOW() AT TIME ZONE 'UTC')
                   OR (
                       uc.expiration_date IS NULL
                       AND pc.end_time IS NOT NULL
                       AND pc.end_time < NOW() AT TIME ZONE 'UTC'
                   )
               )
            """
        )

    @api.model
    def search(self, domain, offset=0, limit=None, order=None):
        records = super().search(domain, offset=offset, limit=limit, order=order)
        records._sync_expired_state()
        return records

    def _get_effective_expiration_date(self):
        self.ensure_one()
        return self.expiration_date or self.coupon_id.end_time

    def _sync_expired_state(self):
        now = fields.Datetime.now()
        to_expire = self.filtered(
            lambda record: record.state in ("redeemed", "activated")
            and not record.is_used
            and record._get_effective_expiration_date()
            and now > record._get_effective_expiration_date()
        )
        if to_expire:
            to_expire.write({"state": "expired"})
        return self

    def action_activate(self):
        self._sync_expired_state()
        now = fields.Datetime.now()
        for record in self:
            if record.state == "used" or record.is_used:
                raise ValidationError("Coupon ถูกใช้แล้ว")
            if record.state == "expired":
                raise ValidationError("Coupon หมดอายุแล้ว")
            if record.state == "activated":
                continue

            pre_activation_expiration = record._get_effective_expiration_date()
            if pre_activation_expiration and now > pre_activation_expiration:
                record.write({"state": "expired"})
                raise ValidationError("Coupon หมดอายุแล้ว")

            expiration_date = False
            if record.coupon_id.code_expiry_interval:
                expiration_date = now + timedelta(minutes=record.coupon_id.code_expiry_interval)

            record.write({
                "state": "activated",
                "activated_date": now,
                "expiration_date": expiration_date,
            })

    def action_mark_used(self):
        self._sync_expired_state()
        now = fields.Datetime.now()
        for record in self:
            if record.is_used:
                continue
            if record.state == "expired":
                raise ValidationError("Coupon หมดอายุแล้ว")
            if record.state != "activated":
                raise ValidationError("กรุณาเปิดใช้งานคูปองก่อน")
            if record.expiration_date and now > record.expiration_date:
                record.write({"state": "expired"})
                raise ValidationError("Coupon หมดอายุแล้ว")
            record.write({
                "state": "used",
                "is_used": True,
                "used_date": now,
            })
            if record.coupon_code_id:
                record.coupon_code_id.write({
                    "state": "used",
                    "used_by_user_id": record.user_id.id,
                    "used_date": now,
                })

    @api.constrains("currency_id", "partner_id", "coupon_id", "user_id")
    def _check_partner_consistency(self):
        for record in self:
            if record.user_id and record.user_id.partner_id != record.partner_id:
                raise ValidationError("User coupon partner must match the user's partner.")
            if record.coupon_id and record.coupon_id.partner_id != record.partner_id:
                raise ValidationError("User coupon partner must match the coupon partner.")
            if record.currency_id and record.currency_id.partner_id != record.partner_id:
                raise ValidationError("User coupon currency must belong to this partner.")
