import json
import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .lazada_integration import (
    LAZADA_QUALIFYING_STATUSES,
    LAZADA_TRANSACTION_LOOKBACK_DAYS,
    LAZADA_TRANSACTION_MAX_PAGES,
    LAZADA_TRANSACTION_PAGE_SIZE,
)

_logger = logging.getLogger(__name__)

DELIVERED_ITEM_STATUS = "delivered"


class PartnerLazadaOrderClaim(models.Model):
    _name = "partner.lazada.order.claim"
    _description = "Partner Lazada Order Claim"
    _order = "claimed_at desc"
    _sql_constraints = [
        (
            "partner_lazada_order_claim_uniq",
            "unique(partner_id, order_number)",
            "This Lazada order number has already been claimed.",
        ),
    ]

    partner_id = fields.Many2one("partner", string="Partner", required=True, ondelete="cascade", index=True)
    order_number = fields.Char(string="Order Number", required=True)
    lazada_order_id = fields.Char(string="Lazada Order ID")
    user_id = fields.Many2one("crm.user", string="Member", required=True, ondelete="cascade")
    amount = fields.Float(string="Amount (Delivered Items)")
    used_flat_fallback = fields.Boolean(string="Used Flat Fallback", default=False)
    qualifying_item_count = fields.Integer(string="Delivered Item Count")
    total_item_count = fields.Integer(string="Total Item Count")
    item_snapshot = fields.Text(string="Item Snapshot (JSON)")
    spending_point_id = fields.Many2one("crm.user.point", string="Spending Point", readonly=True, ondelete="set null")
    reward_point_id = fields.Many2one("crm.user.point", string="Reward Point", readonly=True, ondelete="set null")
    claimed_at = fields.Datetime(string="Claimed At", default=fields.Datetime.now, required=True)

    @api.constrains("user_id", "partner_id")
    def _check_user_partner(self):
        for record in self:
            if record.user_id.partner_id != record.partner_id:
                raise ValidationError("ผู้ใช้ต้องอยู่ภายใต้ Partner เดียวกัน")

    # ------------------------------------------------------------------
    @api.model
    def verify_order_for_points(self, partner, user, order_number):
        order_number = (order_number or "").strip()
        if not order_number:
            raise ValidationError("กรุณาระบุหมายเลขคำสั่งซื้อ")

        if self.search_count([("partner_id", "=", partner.id), ("order_number", "=", order_number)]):
            raise ValidationError(f"คำสั่งซื้อ '{order_number}' ถูกใช้ขอแต้มไปแล้ว")

        partner._ensure_lazada_ready()

        order = self._find_lazada_order(partner, order_number)
        if not order:
            raise ValidationError("ไม่พบคำสั่งซื้อนี้ในระบบ")

        items = order.get("member_sub_order_list") or []
        qualifying_items = [
            item for item in items
            if str(item.get("status") or "").strip().lower() == DELIVERED_ITEM_STATUS
        ]
        if not qualifying_items:
            raise ValidationError("ยังไม่มีสินค้าในคำสั่งซื้อนี้ที่จัดส่งสำเร็จ")

        amount = sum(
            self._parse_amount(item.get("paid_price") if item.get("paid_price") is not None else item.get("item_price"))
            for item in qualifying_items
        )

        # Grant the points and create the dedup ledger row atomically: if the
        # unique constraint on (partner_id, order_number) rejects a
        # concurrent duplicate, the points just created must roll back too.
        try:
            with self.env.cr.savepoint():
                spending_point, reward_point, _reward_value, used_flat_fallback = self._grant_points(
                    partner, user, amount
                )
                claim = self.create({
                    "partner_id": partner.id,
                    "order_number": order_number,
                    "lazada_order_id": str(order.get("order_id") or "") or False,
                    "user_id": user.id,
                    "amount": amount,
                    "used_flat_fallback": used_flat_fallback,
                    "qualifying_item_count": len(qualifying_items),
                    "total_item_count": len(items),
                    "item_snapshot": json.dumps(qualifying_items, ensure_ascii=False, default=str),
                    "spending_point_id": spending_point.id if spending_point else False,
                    "reward_point_id": reward_point.id if reward_point else False,
                    "claimed_at": fields.Datetime.now(),
                })
        except IntegrityError as error:
            raise ValidationError(f"คำสั่งซื้อ '{order_number}' ถูกใช้ขอแต้มไปแล้ว") from error

        return claim

    def _find_lazada_order(self, partner, order_number):
        today = fields.Date.context_today(self)
        created_after = fields.Date.to_string(today - timedelta(days=LAZADA_TRANSACTION_LOOKBACK_DAYS))

        for status in LAZADA_QUALIFYING_STATUSES:
            offset = 0
            for _page in range(LAZADA_TRANSACTION_MAX_PAGES):
                orders = partner.fetch_lazada_transactions(
                    status=status,
                    created_after=created_after,
                    offset=offset,
                    limit=LAZADA_TRANSACTION_PAGE_SIZE,
                )
                if not orders:
                    break
                for order in orders:
                    if str(order.get("order_number") or "").strip() == order_number:
                        return order
                if len(orders) < LAZADA_TRANSACTION_PAGE_SIZE:
                    break
                offset += LAZADA_TRANSACTION_PAGE_SIZE
        return False

    @staticmethod
    def _parse_amount(value):
        try:
            return max(float(value or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _grant_points(self, partner, user, amount):
        """Returns (spending_point, reward_point, reward_value, used_flat_fallback)."""
        now = fields.Datetime.now()
        point_model = self.env["crm.user.point"].sudo()

        if amount > 0:
            spending_currency = partner._get_spending_currency()
            default_currency = partner._get_default_point_currency()
            if not spending_currency or not default_currency:
                raise ValidationError("Partner ยังไม่ได้ตั้งค่า currency สำหรับคะแนน")

            convert_points = partner._get_user_convert_points(user)
            if convert_points <= 0:
                tier_name = user.tier_id.name if user.tier_id else "-"
                raise ValidationError(f"Tier '{tier_name}' ยังไม่ได้ตั้งค่า Convert Points")
            reward_value = partner._calculate_reward_points(amount, convert_points)

            spending_point = point_model.create({
                "name": "คะแนนจาก Lazada",
                "admin_note": "Lazada order claim",
                "value": amount,
                "type": "earn",
                "given_date": now,
                "currency_id": spending_currency.id,
                "user_id": user.id,
            })
            reward_point = False
            if reward_value > 0:
                reward_point = point_model.create({
                    "name": "คะแนนจาก Lazada",
                    "admin_note": (
                        f"Lazada order claim ({amount:g} / {convert_points:g} = {reward_value:g} points)"
                    ),
                    "value": reward_value,
                    "type": "earn",
                    "given_date": now,
                    "currency_id": default_currency.id,
                    "user_id": user.id,
                })
            return spending_point, reward_point, reward_value, False

        if partner.lazada_flat_point_value <= 0:
            raise ValidationError(
                "ไม่พบยอดเงินของคำสั่งซื้อ และยังไม่ได้ตั้งค่าแต้มสำรอง (flat point) กรุณาติดต่อผู้ดูแลระบบ"
            )
        default_currency = partner._get_default_point_currency()
        if not default_currency:
            raise ValidationError("Partner ยังไม่ได้ตั้งค่า currency สำหรับคะแนน")

        reward_point = point_model.create({
            "name": "คะแนนจาก Lazada",
            "admin_note": "Lazada order claim (flat point fallback)",
            "value": partner.lazada_flat_point_value,
            "type": "earn",
            "given_date": now,
            "currency_id": default_currency.id,
            "user_id": user.id,
        })
        return False, reward_point, partner.lazada_flat_point_value, True

    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "order_number": self.order_number,
            "lazada_order_id": self.lazada_order_id or False,
            "user_id": self.user_id.id,
            "display_name": self.user_id.display_name,
            "amount": self.amount,
            "used_flat_fallback": self.used_flat_fallback,
            "qualifying_item_count": self.qualifying_item_count,
            "total_item_count": self.total_item_count,
            "reward_value": self.reward_point_id.value if self.reward_point_id else self.spending_point_id.value if self.spending_point_id else 0,
            "claimed_at": fields.Datetime.to_string(self.claimed_at),
        }
