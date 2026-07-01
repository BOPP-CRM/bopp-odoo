from html import escape
from urllib.parse import quote
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerPointRedeem(models.Model):
    _name = "crm.partner.point.redeem"
    _description = "Point Redeem"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _sql_constraints = [
        ("point_redeem_code_uniq", "unique(code)", "Redeem code must be unique."),
    ]

    name = fields.Char(string="Name", required=True, tracking=True)
    value = fields.Float(string="Value", required=True, tracking=True)
    code = fields.Char(
        string="Code",
        tracking=True,
        required=True,
        default=lambda self: self._generate_code(),
        copy=False,
        readonly=True,
    )
    type = fields.Selection([
        ("earn", "Earn"),
        ("transfer", "Transfer"),
        ("burn", "Burn"),
    ], string="Type", required=True, default="earn", tracking=True)
    limit_per_user = fields.Integer(string="Limit per user", default=1, tracking=True)
    limit_per_qr = fields.Integer(string="Limit per QR", default=1, tracking=True)
    expiration_date = fields.Datetime(string="Expiration Date", tracking=True)
    active = fields.Boolean(string="Active", default=True, tracking=True)

    redeem_url = fields.Char(
        string="Redeem URL",
        compute="_compute_redeem_url",
    )
    qr_code_preview = fields.Html(
        string="QR Code",
        compute="_compute_qr_code_preview",
        sanitize=False,
    )
    redeemed_count = fields.Integer(
        string="Redeemed Count",
        compute="_compute_redeemed_count",
    )

    reward_coupon_id = fields.Many2one(
        "partner.coupon",
        string="Reward Coupon",
        tracking=True,
        domain="[('partner_id', '=', partner_id)]",
        help="คูปองที่จะมอบให้ผู้ใช้เมื่อ redeem QR สำเร็จ",
    )

    point_ids = fields.One2many(
        "crm.user.point",
        "point_redeem_id",
        string="Point Redeem",
    )
    user_coupon_ids = fields.One2many(
        "crm.user.coupon",
        "point_redeem_id",
        string="Reward Coupons",
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
        ondelete="restrict",
    )

    @api.model
    def _generate_code(self):
        return secrets.token_urlsafe(16)

    @api.depends("code", "partner_id.slug", "partner_id.partner_line_liff_id")
    def _compute_redeem_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for record in self:
            if not record.code or not record.partner_id:
                record.redeem_url = False
                continue

            liff_id = record.partner_id.partner_line_liff_id
            if liff_id:
                record.redeem_url = (
                    f"https://liff.line.me/{quote(liff_id)}"
                    f"?redeem_code={quote(record.code)}"
                    f"&partner={quote(record.partner_id.slug or '')}"
                )
                continue

            record.redeem_url = (
                f"{base_url}/api/partner/{quote(record.partner_id.slug or '')}"
                f"/redeem/{quote(record.code)}"
            )

    @api.depends("redeem_url")
    def _compute_qr_code_preview(self):
        for record in self:
            if not record.redeem_url:
                record.qr_code_preview = False
                continue

            qr_url = record.get_qr_code_url()
            record.qr_code_preview = (
                f'<img src="{escape(qr_url, quote=True)}" '
                'style="width:220px;height:220px;" alt="Redeem QR Code"/>'
            )

    def get_qr_code_url(self, width=220, height=220):
        self.ensure_one()
        if not self.redeem_url:
            return False

        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        encoded_url = quote(self.redeem_url, safe="")
        return (
            f"{base_url}/report/barcode/QR/{encoded_url}"
            f"?width={width}&height={height}"
        )

    @api.depends("point_ids", "user_coupon_ids")
    def _compute_redeemed_count(self):
        for record in self:
            record.redeemed_count = max(
                len(record.point_ids),
                len(record.user_coupon_ids),
            )

    def redeem_for_user(self, user):
        self.ensure_one()
        self._check_can_redeem(user)

        point = False
        if self.value > 0:
            point = self.env["crm.user.point"].sudo().create({
                "name": self.name,
                "value": self.value,
                "type": self.type,
                "given_date": fields.Datetime.now(),
                "currency_id": self.currency_id.id,
                "user_id": user.id,
                "point_redeem_id": self.id,
            })

        user_coupon = False
        if self.reward_coupon_id:
            user_coupon = self.reward_coupon_id.grant_to_user(
                user,
                f"Redeem QR: {self.name}",
                point_redeem_id=self.id,
            )

        return {
            "point": point,
            "user_coupon": user_coupon,
        }

    def _get_user_redeem_count(self, user):
        self.ensure_one()
        point_count = self.env["crm.user.point"].sudo().search_count([
            ("point_redeem_id", "=", self.id),
            ("user_id", "=", user.id),
        ])
        coupon_count = self.env["crm.user.coupon"].sudo().search_count([
            ("point_redeem_id", "=", self.id),
            ("user_id", "=", user.id),
        ])
        return max(point_count, coupon_count)

    def _check_can_redeem(self, user):
        self.ensure_one()
        if not self.active:
            raise ValidationError("Redeem QR code is inactive.")

        if self.expiration_date and fields.Datetime.now() > self.expiration_date:
            raise ValidationError("Redeem QR code is expired.")

        if user.partner_id != self.partner_id:
            raise ValidationError("User does not belong to this partner.")

        if self.currency_id.partner_id != self.partner_id:
            raise ValidationError("Point currency must belong to this partner.")

        if self.value <= 0 and not self.reward_coupon_id:
            raise ValidationError("ต้องระบุ point value หรือ reward coupon อย่างน้อย 1 รายการ")

        if self.reward_coupon_id:
            if self.reward_coupon_id.partner_id != self.partner_id:
                raise ValidationError("Reward coupon ต้องอยู่ใน Partner เดียวกัน")
            available_count = self.env["partner.coupon.code"].sudo().search_count([
                ("coupon_id", "=", self.reward_coupon_id.id),
                ("state", "=", "available"),
            ])
            if not available_count:
                raise ValidationError("คูปองรางวัลหมดแล้ว")

        if self.limit_per_qr and self.redeemed_count >= self.limit_per_qr:
            raise ValidationError("Redeem QR code has reached its limit.")

        user_redeem_count = self._get_user_redeem_count(user)
        if self.limit_per_user and user_redeem_count >= self.limit_per_user:
            raise ValidationError("User has reached the redeem limit.")

    @api.constrains("value", "reward_coupon_id")
    def _check_reward_configuration(self):
        for record in self:
            if record.value < 0:
                raise ValidationError("Value ต้องมากกว่าหรือเท่ากับ 0")
            if record.value <= 0 and not record.reward_coupon_id:
                raise ValidationError("ต้องระบุ point value หรือ reward coupon อย่างน้อย 1 รายการ")

    @api.constrains("reward_coupon_id", "partner_id")
    def _check_reward_coupon_partner(self):
        for record in self:
            if (
                record.reward_coupon_id
                and record.partner_id
                and record.reward_coupon_id.partner_id != record.partner_id
            ):
                raise ValidationError("Reward coupon ต้องอยู่ใน Partner เดียวกัน")

    @api.constrains("limit_per_user", "limit_per_qr")
    def _check_limits(self):
        for record in self:
            if record.limit_per_user < 0 or record.limit_per_qr < 0:
                raise ValidationError("Limits must be zero or greater.")

    @api.constrains("currency_id", "partner_id")
    def _check_currency_partner(self):
        for record in self:
            if (
                record.currency_id
                and record.partner_id
                and record.currency_id.partner_id != record.partner_id
            ):
                raise ValidationError("Point currency must belong to this partner.")
