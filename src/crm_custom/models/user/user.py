from html import escape

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class User(models.Model):
    _name = "crm.user"
    _description = "User"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _sql_constraints = [
        ("user_line_user_id_uniq", "unique(line_user_id, partner_id)", "LINE user ID and Partner combination must be unique."),
    ]

    display_name = fields.Char(string="Display Name", tracking=True, required=True)
    picture_url = fields.Char(string="Picture URL", tracking=True, required=True)
    picture_preview = fields.Html(
        string="Picture",
        compute="_compute_picture_preview",
        sanitize=False,
    )
    line_user_id = fields.Char(string="LINE UUID", tracking=True, required=True)

    email = fields.Char(string="Email", tracking=True)
    is_email_verified = fields.Boolean(string="Is Email Verified", tracking=True)

    phone = fields.Char(string="Phone", tracking=True)
    is_phone_verified = fields.Boolean(string="Is Phone Verified", tracking=True)

    birth_date = fields.Date(string="Birth Date", tracking=True)
    gender = fields.Selection([
        ("M", "Male"),
        ("F", "Female"),
        ("O", "Other")
    ], string="Gender", tracking=True)
    address = fields.Text(string="Address", tracking=True)
    is_updated_user_info = fields.Boolean(
        string="Is Updated User Info",
        default=False,
        tracking=True,
    )

    active = fields.Boolean(string="Active", default=True, tracking=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    tier_id = fields.Many2one(
        "partner.tier",
        string="Tier",
        tracking=True,
        domain="[('partner_id', '=', partner_id)]",
    )

    point_ids = fields.One2many(
        "crm.user.point",
        "user_id",
        string="Points",
    )

    coupon_ids = fields.One2many(
        "crm.user.coupon",
        "user_id",
        string="Coupons",
    )

    point_balance_display = fields.Char(
        string="Point Balance",
        compute="_compute_user_summaries",
    )
    coupon_count = fields.Integer(
        string="Coupons",
        compute="_compute_user_summaries",
    )
    active_coupon_count = fields.Integer(
        string="Active Coupons",
        compute="_compute_user_summaries",
    )
    coupon_summary_display = fields.Char(
        string="Coupon Codes",
        compute="_compute_user_summaries",
    )

    @api.depends("point_ids", "point_ids.value", "point_ids.type", "point_ids.currency_id", "coupon_ids", "coupon_ids.is_used", "coupon_ids.code")
    def _compute_user_summaries(self):
        for record in self:
            balance_parts = []
            for currency in record.partner_id.currency_ids:
                balance = record._get_currency_balance(currency)
                balance_parts.append(f"{currency.name}: {balance:g}")

            record.point_balance_display = ", ".join(balance_parts) if balance_parts else "-"
            record.coupon_count = len(record.coupon_ids)
            active_coupons = record.coupon_ids.filtered(lambda coupon: not coupon.is_used)
            record.active_coupon_count = len(active_coupons)

            coupon_codes = active_coupons.mapped("code")
            if not coupon_codes:
                record.coupon_summary_display = "-"
            elif len(coupon_codes) <= 5:
                record.coupon_summary_display = ", ".join(coupon_codes)
            else:
                preview = ", ".join(coupon_codes[:5])
                record.coupon_summary_display = f"{preview} (+{len(coupon_codes) - 5})"

    def _get_currency_balance(self, currency):
        self.ensure_one()
        points = self.point_ids.filtered(lambda point: point.currency_id == currency)
        earn = sum(points.filtered(lambda point: point.type == "earn").mapped("value"))
        transfer = sum(points.filtered(lambda point: point.type == "transfer").mapped("value"))
        burn = sum(points.filtered(lambda point: point.type == "burn").mapped("value"))
        return earn - transfer - burn

    def _get_total_spending_currency(self):
        self.ensure_one()
        return self.partner_id.currency_ids.filtered("is_total_spending")[:1]

    def _get_tier_for_spending(self, spending):
        self.ensure_one()
        partner_tier_domain = [("partner_id", "=", self.partner_id.id)]

        tier = self.env["partner.tier"].search(
            partner_tier_domain + [
                ("min_spending", "<=", spending),
                ("max_spending", ">=", spending),
            ],
            order="min_spending desc",
            limit=1,
        )
        if tier:
            return tier

        highest_tier = self.env["partner.tier"].search(
            partner_tier_domain,
            order="max_spending desc, min_spending desc",
            limit=1,
        )
        if highest_tier and spending > highest_tier.max_spending:
            return highest_tier

        return self.env["partner.tier"].search(
            partner_tier_domain,
            order="min_spending asc",
            limit=1,
        )

    def _get_total_spending_balance(self):
        self.ensure_one()
        spending_currency = self._get_total_spending_currency()
        if not spending_currency:
            return 0
        return self._get_currency_balance(spending_currency)

    def _update_tier(self):
        for record in self:
            spending = record._get_total_spending_balance()
            tier = record._get_tier_for_spending(spending)
            if record.tier_id != tier:
                old_tier = record.tier_id
                record.tier_id = tier
                record._grant_tier_promotion_rewards(old_tier, tier)

    def _has_member_reward_grant(self, reward):
        self.ensure_one()
        if reward.reward_type == "point":
            return bool(self.point_ids.filtered(lambda point: point.member_reward_id == reward))
        return bool(self.coupon_ids.filtered(lambda coupon: coupon.member_reward_id == reward))

    def _grant_member_reward_point(self, reward, event, tier=False):
        self.ensure_one()
        if self._has_member_reward_grant(reward):
            return False

        self.env["crm.user.point"].create({
            "name": reward.name,
            "admin_note": reward.name,
            "value": reward.point_value,
            "type": "earn",
            "given_date": fields.Datetime.now(),
            "currency_id": reward.point_currency_id.id,
            "user_id": self.id,
            "member_reward_id": reward.id,
            "member_reward_event": event,
            "member_reward_tier_id": tier.id if tier else False,
        })
        return True

    def _grant_member_reward_coupon(self, reward, event, tier=False):
        self.ensure_one()
        if self._has_member_reward_grant(reward):
            return False

        try:
            user_coupon = reward.coupon_id.grant_to_user(self, reward.name)
        except ValidationError:
            return False

        user_coupon.write({
            "member_reward_id": reward.id,
            "member_reward_event": event,
            "member_reward_tier_id": tier.id if tier else False,
        })
        return True

    def _grant_member_rewards(self, rewards, event, tier=False):
        self.ensure_one()
        for reward in rewards:
            if reward.reward_type == "point":
                self._grant_member_reward_point(reward, event, tier=tier)
            elif reward.reward_type == "coupon":
                self._grant_member_reward_coupon(reward, event, tier=tier)

    def _grant_join_rewards(self):
        for record in self:
            record._grant_member_rewards(
                record.partner_id.join_reward_ids,
                "join",
            )

    def _grant_tier_promotion_rewards(self, old_tier, new_tier):
        self.ensure_one()
        if not new_tier or not old_tier:
            return
        if new_tier.min_spending <= old_tier.min_spending:
            return

        self._grant_member_rewards(
            new_tier.promotion_reward_ids,
            "tier_promotion",
            tier=new_tier,
        )

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._update_tier()
        users._grant_join_rewards()
        return users

    def adjust_point(self, value, point_type, currency, note, expiration_date=None):
        self.ensure_one()
        if not note or not note.strip():
            raise ValidationError("กรุณาระบุหมายเหตุ")
        if value <= 0:
            raise ValidationError("Value ต้องมากกว่า 0")
        if currency.partner_id != self.partner_id:
            raise ValidationError("Point currency must belong to this partner.")

        return self.env["crm.user.point"].create({
            "name": "คะแนนจากแอดมิน",
            "admin_note": f"{self.env.user.display_name}: {note.strip()}",
            "value": value,
            "type": point_type,
            "given_date": fields.Datetime.now(),
            "expiration_date": expiration_date,
            "currency_id": currency.id,
            "user_id": self.id,
        })

    def action_open_user(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "User",
            "res_model": "crm.user",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_add_point(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Add Point",
            "res_model": "crm.user.add.point.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_user_id": self.id,
                "default_partner_id": self.partner_id.id,
            },
        }

    def action_add_coupon(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Add Coupon",
            "res_model": "crm.user.add.coupon.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_user_id": self.id,
                "default_partner_id": self.partner_id.id,
            },
        }

    @api.depends("picture_url")
    def _compute_picture_preview(self):
        for record in self:
            if not record.picture_url:
                record.picture_preview = False
                continue

            picture_url = escape(record.picture_url, quote=True)
            record.picture_preview = (
                f'<img src="{picture_url}" '
                'style="width:128px;height:128px;object-fit:cover;border-radius:50%;" '
                'alt="User picture"/>'
            )
