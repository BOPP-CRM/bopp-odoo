from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerOmisellOrderClaim(models.Model):
    _name = "partner.omisell.order.claim"
    _description = "Partner Omisell Order Claim"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "crm.user",
        string="User",
        required=True,
        ondelete="cascade",
    )
    platform = fields.Selection(
        [
            ("shopee", "Shopee"),
            ("lazada", "Lazada"),
            ("tiktok", "TikTok"),
            ("other", "Other"),
        ],
        string="Platform",
        required=True,
        tracking=True,
    )
    order_number = fields.Char(string="Order Number", required=True, tracking=True)
    omisell_order_id = fields.Many2one(
        "partner.omisell.order",
        string="Omisell Order",
        readonly=True,
        ondelete="set null",
        copy=False,
        index=True,
    )
    omisell_order_number = fields.Char(string="Omisell Order Number", readonly=True, copy=False)
    order_status_id = fields.Integer(string="Order Status ID", readonly=True)
    order_status_name = fields.Char(string="Order Status", readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
        string="Status",
        default="pending",
        required=True,
        tracking=True,
    )
    reject_reason = fields.Text(string="Reject Reason", tracking=True)
    error_message = fields.Text(string="Error Message", tracking=True)
    submitted_date = fields.Datetime(
        string="Submitted Date",
        default=fields.Datetime.now,
        required=True,
        tracking=True,
    )
    reviewed_date = fields.Datetime(string="Reviewed Date", tracking=True)
    reviewed_by_id = fields.Many2one("res.users", string="Reviewed By", tracking=True)
    last_checked_at = fields.Datetime(string="Last Checked At", readonly=True)
    spending_point_id = fields.Many2one(
        "crm.user.point",
        string="Spending Point",
        readonly=True,
        ondelete="set null",
    )
    reward_point_id = fields.Many2one(
        "crm.user.point",
        string="Reward Point",
        readonly=True,
        ondelete="set null",
    )

    @api.constrains("user_id", "partner_id")
    def _check_user_partner(self):
        for record in self:
            if record.user_id and record.partner_id and record.user_id.partner_id != record.partner_id:
                raise ValidationError("ผู้ใช้ต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.model
    def _find_duplicate_claim(self, partner, order_number, exclude_id=None):
        order_number = (order_number or "").strip()
        if not order_number:
            return self.browse()

        domain = [
            ("partner_id", "=", partner.id),
            ("order_number", "=ilike", order_number),
            ("state", "!=", "rejected"),
        ]
        if exclude_id:
            domain.append(("id", "!=", exclude_id))
        return self.search(domain, limit=1)

    @api.model
    def validate_claim_available(self, partner, order_number, exclude_claim_id=None):
        duplicate = self._find_duplicate_claim(partner, order_number, exclude_claim_id)
        if not duplicate:
            return

        normalized_number = (order_number or "").strip()
        if duplicate.state == "approved":
            raise ValidationError(
                f"คำสั่งซื้อ '{normalized_number}' ถูกใช้ขอคะแนนไปแล้ว"
            )
        raise ValidationError(
            f"คำสั่งซื้อ '{normalized_number}' อยู่ระหว่างตรวจสอบแล้ว"
        )

    @api.constrains("order_number", "partner_id", "state")
    def _check_unique_claim(self):
        for record in self:
            if record.state == "rejected" or not record.order_number:
                continue
            self.validate_claim_available(record.partner_id, record.order_number, record.id)

    @api.model
    def submit_claim(self, partner, user, platform, order_number):
        order_number = (order_number or "").strip()
        platform = (platform or "").strip().lower()
        if not order_number:
            raise ValidationError("กรุณาระบุหมายเลขคำสั่งซื้อ")

        valid_platforms = dict(self._fields["platform"].selection)
        if platform not in valid_platforms:
            raise ValidationError("กรุณาเลือกช่องทางการสั่งซื้อให้ถูกต้อง")

        self.validate_claim_available(partner, order_number)

        claim = self.create({
            "partner_id": partner.id,
            "user_id": user.id,
            "platform": platform,
            "order_number": order_number,
            "submitted_date": fields.Datetime.now(),
            "state": "pending",
        })
        claim._check_and_award(raise_on_pending=True)
        return claim

    def recheck(self):
        self.ensure_one()
        if self.state != "pending":
            raise ValidationError("ตรวจสอบซ้ำได้เฉพาะรายการที่รอดำเนินการ")
        self._check_and_award(raise_on_pending=True)
        return self

    def action_reject(self):
        for record in self:
            record._reject()

    def _reject(self):
        self.ensure_one()
        if self.state != "pending":
            raise ValidationError("สามารถปฏิเสธได้เฉพาะรายการที่รอตรวจสอบ")
        self.write({
            "state": "rejected",
            "reviewed_date": fields.Datetime.now(),
            "reviewed_by_id": self.env.user.id,
        })

    def _find_matching_order(self):
        self.ensure_one()
        order_model = self.env["partner.omisell.order"].sudo()
        order = order_model.search([
            ("partner_id", "=", self.partner_id.id),
            ("order_number", "=ilike", self.order_number),
            ("platform", "=ilike", self.platform),
        ], limit=1)
        if order:
            return order
        return order_model.search([
            ("partner_id", "=", self.partner_id.id),
            ("order_number", "=ilike", self.order_number),
        ], limit=1)

    def _mark_approved(self, order):
        self.ensure_one()
        self.write({
            "state": "approved",
            "reviewed_date": fields.Datetime.now(),
            "spending_point_id": order.spending_point_id.id if order.spending_point_id else False,
            "reward_point_id": order.reward_point_id.id if order.reward_point_id else False,
            "error_message": False,
        })

    def _check_and_award(self, raise_on_pending=False):
        self.ensure_one()
        if self.state != "pending":
            return

        order = self.omisell_order_id
        if not order:
            order = self._find_matching_order()
            if not order:
                raise ValidationError(
                    "ไม่พบคำสั่งซื้อนี้ในระบบ กรุณาตรวจสอบช่องทางและหมายเลขคำสั่งซื้ออีกครั้ง"
                )
            self.write({
                "omisell_order_id": order.id,
                "omisell_order_number": order.omisell_order_number,
            })

        result = order.refresh_and_award(target_user=self.user_id)
        self.write({
            "order_status_id": order.order_status_id,
            "order_status_name": order.order_status_name,
            "last_checked_at": fields.Datetime.now(),
        })
        if result.get("status") == "error":
            msg = result.get("message") or "ไม่สามารถตรวจสอบสถานะคำสั่งซื้อได้"
            self.write({"error_message": msg})
            if raise_on_pending:
                raise ValidationError(msg)
            return

        if result.get("points_revoked") is not None:
            self.write({
                "state": "rejected",
                "reviewed_date": fields.Datetime.now(),
                "reject_reason": "คำสั่งซื้อนี้ถูกยกเลิกหรือคืนสินค้า",
            })
            if raise_on_pending:
                raise ValidationError("คำสั่งซื้อนี้ถูกยกเลิกหรือคืนสินค้า")
            return

        reason = result.get("reason")
        if reason == "member_mismatch":
            self.write({
                "state": "rejected",
                "reviewed_date": fields.Datetime.now(),
                "reject_reason": "ข้อมูลผู้รับสินค้าของคำสั่งซื้อนี้ไม่ตรงกับสมาชิกที่ขอคะแนน",
            })
            if raise_on_pending:
                raise ValidationError("ข้อมูลผู้รับสินค้าของคำสั่งซื้อนี้ไม่ตรงกับสมาชิกที่ขอคะแนน")
            return

        if reason == "return_blocked":
            self.write({
                "state": "rejected",
                "reviewed_date": fields.Datetime.now(),
                "reject_reason": "คำสั่งซื้อนี้มีการคืนสินค้า",
            })
            if raise_on_pending:
                raise ValidationError("คำสั่งซื้อนี้มีการคืนสินค้า")
            return

        if reason == "not_completed":
            msg = "คำสั่งซื้อยังไม่เสร็จสมบูรณ์ กรุณาลองเช็คใหม่อีกครั้งภายหลัง"
            self.write({"error_message": msg})
            if raise_on_pending:
                raise ValidationError(msg)
            return

        if reason == "member_not_found":
            msg = "ไม่พบข้อมูลสมาชิกจากคำสั่งซื้อนี้ กรุณาติดต่อเจ้าหน้าที่"
            self.write({"error_message": msg})
            if raise_on_pending:
                raise ValidationError(msg)
            return

        if result.get("points_awarded") or result.get("already_awarded"):
            self._mark_approved(order)
            return

        msg = "ไม่สามารถให้คะแนนได้ กรุณาติดต่อเจ้าหน้าที่"
        self.write({"error_message": msg})
        if raise_on_pending:
            raise ValidationError(msg)

    @api.model
    def _resolve_pending_claims_for_order(self, order):
        claims = self.search([
            ("omisell_order_id", "=", order.id),
            ("state", "=", "pending"),
        ])
        for claim in claims:
            claim._check_and_award()
