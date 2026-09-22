import logging
from datetime import date, datetime

import pytz
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

BANGKOK_TZ = pytz.timezone("Asia/Bangkok")


class PartnerBirthdayReward(models.Model):
    _name = "partner.birthday.reward"
    _description = "Partner Birthday Reward"
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
        string="Birthday Coupon",
        required=True,
        ondelete="restrict",
        domain="[('partner_id', '=', partner_id)]",
        help="ควรตั้งคูปองนี้ให้ Show In UI = ปิด เพื่อไม่ให้โผล่ในรายการแลกด้วยแต้มปกติ",
    )
    min_membership_days = fields.Integer(
        string="Min Membership Days",
        default=0,
        help="สมาชิกต้องสมัครมาแล้วอย่างน้อยกี่วันถึงจะเก็บคูปองวันเกิดได้",
    )
    message_type = fields.Selection(
        [("text", "Text"), ("flex", "Flex")],
        string="Reminder Message Type",
        default="text",
        required=True,
    )
    message_text = fields.Text(string="Reminder Message Text")
    flex_payload = fields.Text(string="Reminder Flex Payload (JSON)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _today_bkk(self):
        return datetime.now(BANGKOK_TZ).date()

    def _membership_cutoff(self, today):
        self.ensure_one()
        return today - relativedelta(days=max(self.min_membership_days, 0))

    def _eligible_user_domain(self, today):
        self.ensure_one()
        # Loose candidate filter; _check_eligibility applies the exact rules.
        cutoff = datetime.combine(self._membership_cutoff(today) + relativedelta(days=1), datetime.min.time())
        return [
            ("partner_id", "=", self.partner_id.id),
            ("active", "=", True),
            ("birth_date", "!=", False),
            ("create_date", "<", fields.Datetime.to_string(cutoff)),
        ]

    def _has_claimed_this_year(self, user, year):
        self.ensure_one()
        return bool(self.env["crm.user.coupon"].sudo().search_count([
            ("user_id", "=", user.id),
            ("birthday_reward_id", "=", self.id),
            ("acquired_date", ">=", date(year, 1, 1)),
        ]))

    def _check_eligibility(self, user, today):
        """Return (claimable: bool, reason: str|False)."""
        self.ensure_one()
        if not self.active:
            return False, "disabled"
        if user.partner_id != self.partner_id:
            return False, "wrong_partner"
        if not user.birth_date:
            return False, "no_birth_date"
        if user.birth_date.month != today.month:
            return False, "not_birth_month"
        if not user.create_date or user.create_date.date() > self._membership_cutoff(today):
            return False, "membership_too_short"
        if self._has_claimed_this_year(user, today.year):
            return False, "already_claimed"
        if self.coupon_id.available_code_count <= 0:
            return False, "coupon_out_of_stock"
        return True, False

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------
    def claim_for_user(self, user):
        self.ensure_one()
        today = self._today_bkk()
        claimable, reason = self._check_eligibility(user, today)
        if not claimable:
            raise ValidationError(self._reason_message(reason))

        user_coupon = self.coupon_id.grant_to_user(user, f"Birthday {today.year}")
        user_coupon.birthday_reward_id = self.id
        return user_coupon

    @staticmethod
    def _reason_message(reason):
        return {
            "disabled": "สิทธิพิเศษวันเกิดยังไม่เปิดใช้งาน",
            "wrong_partner": "ไม่พบสิทธิ์สำหรับสมาชิกรายนี้",
            "no_birth_date": "กรุณาระบุวันเกิดในโปรไฟล์ก่อน",
            "not_birth_month": "คูปองวันเกิดเก็บได้เฉพาะในเดือนเกิดของคุณเท่านั้น",
            "membership_too_short": "คุณยังเป็นสมาชิกไม่ครบระยะเวลาที่กำหนด",
            "already_claimed": "คุณเก็บคูปองวันเกิดของปีนี้ไปแล้ว",
            "coupon_out_of_stock": "คูปองหมดแล้ว",
        }.get(reason, "ไม่สามารถเก็บคูปองวันเกิดได้")

    def serialize_for_user(self, user):
        self.ensure_one()
        today = self._today_bkk()
        claimable, reason = self._check_eligibility(user, today)
        claimed = self._has_claimed_this_year(user, today.year)
        return {
            "enabled": bool(self.active),
            "in_birth_month": bool(user.birth_date and user.birth_date.month == today.month),
            "claimable": claimable,
            "claimed": claimed,
            "reason": reason or False,
            "min_membership_days": self.min_membership_days,
            "coupon": {
                "id": self.coupon_id.id,
                "name": self.coupon_id.name,
                "image_url": self.coupon_id.image or False,
                "value": self.coupon_id.value,
                "term_and_condition": self.coupon_id.term_and_condition or False,
                "end_time": (
                    fields.Datetime.to_string(self.coupon_id.end_time)
                    if self.coupon_id.end_time
                    else False
                ),
            },
        }

    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "coupon_id": self.coupon_id.id,
            "coupon_name": self.coupon_id.name,
            "coupon_available_code_count": self.coupon_id.available_code_count,
            "coupon_is_show_in_ui": self.coupon_id.is_show_in_ui,
            "min_membership_days": self.min_membership_days,
            "message_type": self.message_type,
            "message_text": self.message_text or False,
            "flex_payload": self.flex_payload or False,
        }

    # ------------------------------------------------------------------
    # LINE reminder cron
    # ------------------------------------------------------------------
    @api.model
    def _cron_send_birthday_line_reminders(self):
        today = self._today_bkk()
        broadcast_model = self.env["partner.broadcast"].sudo()

        for rule in self.search([("active", "=", True)]):
            partner = rule.partner_id
            if not partner._is_line_messaging_ready_safe():
                continue

            already = broadcast_model.search_count([
                ("birthday_reward_id", "=", rule.id),
                ("source", "=", "birthday_auto"),
                ("create_date", ">=", fields.Datetime.to_string(
                    datetime.combine(today, datetime.min.time())
                )),
            ])
            if already:
                continue

            candidates = self.env["crm.user"].sudo().search(rule._eligible_user_domain(today))
            users = candidates.filtered(
                lambda user: rule._birthday_matches(user.birth_date, today)
                and not rule._has_claimed_this_year(user, today.year)
            )
            if not users:
                continue

            try:
                broadcast_model._create_broadcast_job(
                    partner,
                    users,
                    {
                        "name": f"{rule.name} ({today.isoformat()})",
                        "message_type": rule.message_type,
                        "message_text": rule.message_text,
                        "flex_payload": rule.flex_payload,
                        "include_coupon": False,
                    },
                    source="birthday_auto",
                    birthday_reward_id=rule.id,
                )
            except ValidationError as error:
                _logger.info("Birthday reminder skipped for rule %s: %s", rule.id, error)
            except Exception:
                _logger.exception("Birthday reminder failed for rule %s", rule.id)

    @staticmethod
    def _birthday_matches(birth_date, today):
        if not birth_date:
            return False
        if birth_date.month == today.month and birth_date.day == today.day:
            return True
        # 29 Feb birthdays fall on 1 Mar in non-leap years.
        if birth_date.month == 2 and birth_date.day == 29 and today.month == 3 and today.day == 1:
            return True
        return False

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("coupon_id", "partner_id")
    def _check_coupon_partner(self):
        for record in self:
            if record.coupon_id and record.coupon_id.partner_id != record.partner_id:
                raise ValidationError("คูปองต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.constrains("min_membership_days")
    def _check_min_membership_days(self):
        for record in self:
            if record.min_membership_days < 0:
                raise ValidationError("Min membership days ต้องไม่ติดลบ")

    @api.constrains("message_type", "flex_payload")
    def _check_message(self):
        for record in self:
            if record.message_type == "flex" and record.flex_payload:
                record.env["partner"]._build_line_messages("flex", record.message_text, record.flex_payload)

    @api.constrains("active", "partner_id")
    def _check_single_active_rule(self):
        for record in self.filtered("active"):
            others = self.search_count([
                ("partner_id", "=", record.partner_id.id),
                ("active", "=", True),
                ("id", "!=", record.id),
            ])
            if others:
                raise ValidationError("แต่ละ Partner มีสิทธิพิเศษวันเกิดที่เปิดใช้งานได้ 1 รายการ")
