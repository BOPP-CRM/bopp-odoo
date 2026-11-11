import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .broadcast import _ANY, _GENDER_SELECTION, _VERIFIED_SELECTION

_logger = logging.getLogger(__name__)


class PartnerCouponDistributionRule(models.Model):
    _name = "partner.coupon.distribution.rule"
    _description = "Partner Coupon Distribution Rule"
    _inherit = ["mail.thread"]
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True, tracking=True)
    active = fields.Boolean(string="Active", default=True, tracking=True)
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        required=True,
        ondelete="restrict",
        domain="[('partner_id', '=', partner_id)]",
        tracking=True,
    )

    trigger = fields.Selection(
        [("manual", "Manual"), ("scheduled", "Scheduled")],
        string="Trigger",
        default="manual",
        required=True,
        help="Scheduled: ระบบแจกให้สมาชิกที่เข้าเงื่อนไขใหม่โดยอัตโนมัติ (ไม่แจกซ้ำคนที่เคยได้จากกฎนี้)",
    )
    schedule_interval_days = fields.Integer(string="Run Every (days)", default=1)
    last_run_at = fields.Datetime(string="Last Run At", readonly=True)
    last_error = fields.Text(string="Last Error", readonly=True)

    seg_gender = fields.Selection(_GENDER_SELECTION, string="Gender", default=_ANY, required=True)
    seg_age_min = fields.Integer(string="Age Min")
    seg_age_max = fields.Integer(string="Age Max")
    seg_tier_ids = fields.Many2many("partner.tier", string="Tiers")
    seg_signup_from = fields.Datetime(string="Signup From")
    seg_signup_to = fields.Datetime(string="Signup To")
    seg_email_verified = fields.Selection(_VERIFIED_SELECTION, string="Email Verified", default=_ANY, required=True)
    seg_phone_verified = fields.Selection(_VERIFIED_SELECTION, string="Phone Verified", default=_ANY, required=True)

    notify_line = fields.Boolean(string="Notify via LINE", default=False)
    message_type = fields.Selection(
        [("text", "Text"), ("flex", "Flex")],
        string="Message Type",
        default="text",
        required=True,
    )
    message_text = fields.Text(string="Message Text")
    flex_payload = fields.Text(string="Flex Payload (JSON)")

    broadcast_ids = fields.One2many("partner.broadcast", "distribution_rule_id", string="Runs")
    granted_count = fields.Integer(string="Coupons Granted", compute="_compute_granted_count")

    # ------------------------------------------------------------------
    # Audience
    # ------------------------------------------------------------------
    def _compute_granted_count(self):
        coupon_model = self.env["crm.user.coupon"].sudo()
        for record in self:
            record.granted_count = coupon_model.search_count([("distribution_rule_id", "=", record.id)]) if record.id else 0

    def _segment_vals(self):
        self.ensure_one()
        return {
            "partner_id": self.partner_id.id,
            "seg_gender": self.seg_gender,
            "seg_age_min": self.seg_age_min,
            "seg_age_max": self.seg_age_max,
            "seg_tier_ids": [(6, 0, self.seg_tier_ids.ids)],
            "seg_signup_from": self.seg_signup_from,
            "seg_signup_to": self.seg_signup_to,
            "seg_email_verified": self.seg_email_verified,
            "seg_phone_verified": self.seg_phone_verified,
        }

    def _build_audience_domain(self):
        self.ensure_one()
        # Reuse the broadcast segmentation on an unsaved record.
        domain = self.env["partner.broadcast"].new(self._segment_vals())._build_audience_domain()
        # Unsaved records (portal preview) fall back to their origin, if any.
        rule_id = self._origin.id or (self.id if isinstance(self.id, int) else False)
        granted_user_ids = (
            self.env["crm.user.coupon"].sudo()
            .search([("distribution_rule_id", "=", rule_id)])
            .mapped("user_id")
            .ids
        ) if rule_id else []
        if granted_user_ids:
            domain.append(("id", "not in", granted_user_ids))
        return domain

    def _resolve_audience(self):
        self.ensure_one()
        return self.env["crm.user"].sudo().search(self._build_audience_domain(), order="id")

    def preview_audience(self, limit=20):
        self.ensure_one()
        domain = self._build_audience_domain()
        user_model = self.env["crm.user"].sudo()
        return {
            "count": user_model.search_count(domain),
            "coupon_available": self.coupon_id.available_code_count,
            "sample": [
                {
                    "id": user.id,
                    "display_name": user.display_name,
                    "gender": user.gender or False,
                    "tier": user.tier_id.name if user.tier_id else False,
                }
                for user in user_model.search(domain, limit=limit, order="id")
            ],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _has_active_job(self):
        self.ensure_one()
        return bool(self.env["partner.broadcast"].sudo().search_count([
            ("distribution_rule_id", "=", self.id),
            ("state", "in", ["pending", "running"]),
        ]))

    def _run(self, source):
        """Create a broadcast job granting the coupon to new matching members.

        Returns the broadcast, or False when nobody is left to receive it.
        """
        self.ensure_one()
        if self._has_active_job():
            raise ValidationError("กฎนี้กำลังแจกคูปองอยู่ กรุณารอให้เสร็จก่อน")

        users = self._resolve_audience()
        if not users:
            return False

        vals = self._segment_vals()
        vals.pop("partner_id")
        vals.update({
            "name": f"{self.name} ({fields.Date.context_today(self).isoformat()})",
            "include_coupon": True,
            "coupon_id": self.coupon_id.id,
            "send_line": self.notify_line,
            "message_type": self.message_type,
            "message_text": self.message_text,
            "flex_payload": self.flex_payload,
        })
        self.write({"last_run_at": fields.Datetime.now(), "last_error": False})
        return self.env["partner.broadcast"].sudo()._create_broadcast_job(
            self.partner_id, users, vals, source=source, distribution_rule_id=self.id
        )

    def action_preview(self):
        self.ensure_one()
        result = self.preview_audience()
        return self._notify(
            "Preview",
            f"ผู้ที่จะได้รับคูปอง {result['count']} คน (โค้ดคงเหลือ {result['coupon_available']} ใบ)",
        )

    def action_run(self):
        self.ensure_one()
        broadcast = self._run("rule_manual")
        if not broadcast:
            return self._notify("แจกคูปอง", "ไม่มีสมาชิกใหม่ที่เข้าเงื่อนไข")
        return self._notify("แจกคูปอง", f"เริ่มแจกคูปองให้ {broadcast.total_recipients} คนแล้ว")

    @staticmethod
    def _notify(title, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": "info", "sticky": False},
        }

    @api.model
    def _cron_run_scheduled_rules(self):
        now = fields.Datetime.now()
        for rule in self.search([("active", "=", True), ("trigger", "=", "scheduled")]):
            if rule.last_run_at and rule.last_run_at + relativedelta(days=rule.schedule_interval_days) > now:
                continue
            if rule._has_active_job():
                continue
            try:
                rule._run("rule_auto")
            except ValidationError as error:
                _logger.info("Coupon distribution rule %s skipped: %s", rule.id, error)
                rule.last_error = str(error)
            except Exception:
                _logger.exception("Coupon distribution rule %s failed", rule.id)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "coupon": {
                "id": self.coupon_id.id,
                "name": self.coupon_id.name,
                "available_code_count": self.coupon_id.available_code_count,
            },
            "trigger": self.trigger,
            "schedule_interval_days": self.schedule_interval_days,
            "last_run_at": fields.Datetime.to_string(self.last_run_at) if self.last_run_at else False,
            "last_error": self.last_error or False,
            "seg_gender": self.seg_gender,
            "seg_age_min": self.seg_age_min,
            "seg_age_max": self.seg_age_max,
            "seg_tiers": [{"id": tier.id, "name": tier.name} for tier in self.seg_tier_ids],
            "seg_signup_from": fields.Datetime.to_string(self.seg_signup_from) if self.seg_signup_from else False,
            "seg_signup_to": fields.Datetime.to_string(self.seg_signup_to) if self.seg_signup_to else False,
            "seg_email_verified": self.seg_email_verified,
            "seg_phone_verified": self.seg_phone_verified,
            "notify_line": self.notify_line,
            "message_type": self.message_type,
            "message_text": self.message_text or False,
            "flex_payload": self.flex_payload or False,
            "granted_count": self.granted_count,
            "has_active_job": self._has_active_job(),
            "create_date": fields.Datetime.to_string(self.create_date),
        }

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("coupon_id", "seg_tier_ids", "partner_id")
    def _check_partner_consistency(self):
        for record in self:
            if record.coupon_id.partner_id != record.partner_id:
                raise ValidationError("คูปองต้องอยู่ภายใต้ Partner เดียวกัน")
            if record.seg_tier_ids.filtered(lambda tier: tier.partner_id != record.partner_id):
                raise ValidationError("Tier ที่เลือกต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.constrains("seg_age_min", "seg_age_max")
    def _check_age_range(self):
        for record in self:
            if record.seg_age_min < 0 or record.seg_age_max < 0:
                raise ValidationError("อายุต้องไม่ติดลบ")
            if record.seg_age_min and record.seg_age_max and record.seg_age_min > record.seg_age_max:
                raise ValidationError("อายุขั้นต่ำต้องไม่มากกว่าอายุสูงสุด")

    @api.constrains("schedule_interval_days")
    def _check_schedule_interval(self):
        for record in self:
            if record.schedule_interval_days < 1:
                raise ValidationError("ความถี่ในการรันต้องอย่างน้อย 1 วัน")

    @api.constrains("notify_line", "message_type", "message_text", "flex_payload")
    def _check_message(self):
        for record in self.filtered("notify_line"):
            if record.message_type == "text" and not (record.message_text or "").strip():
                raise ValidationError("กรุณาระบุข้อความที่จะส่งทาง LINE")
            if record.message_type == "flex":
                record.env["partner"]._build_line_messages("flex", record.message_text, record.flex_payload)
