import math
import secrets

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerReceiptRedeem(models.Model):
    _name = "crm.partner.receipt.redeem"
    _description = "Partner Receipt Redeem"
    _inherit = ["s3.image.mixin", "mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    receipt_number = fields.Char(string="Receipt Number", required=True, tracking=True)
    receipt_image = fields.Char(string="Receipt Image", tracking=True)
    receipt_image_file = fields.Image(
        string="Receipt Image",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_receipt_image_file",
        inverse="_inverse_receipt_image_file",
    )
    amount = fields.Float(string="Amount", tracking=True)
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
    submitted_date = fields.Datetime(
        string="Submitted Date",
        default=fields.Datetime.now,
        required=True,
        tracking=True,
    )
    reviewed_date = fields.Datetime(string="Reviewed Date", tracking=True)
    reviewed_by_id = fields.Many2one("res.users", string="Reviewed By", tracking=True)

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
    user_tier_id = fields.Many2one(
        "partner.tier",
        string="User Tier",
        related="user_id.tier_id",
        readonly=True,
    )
    tier_convert_points = fields.Float(
        string="Tier Convert Points",
        readonly=True,
        copy=False,
    )
    reward_points = fields.Float(
        string="Reward Points",
        readonly=True,
        copy=False,
    )

    def _get_s3_image_config(self):
        return {
            "receipt_image": {
                "max_width": 1920,
                "max_height": 1920,
            },
        }

    @api.depends("receipt_image")
    def _compute_receipt_image_file(self):
        self._compute_s3_image_file("receipt_image")

    def _inverse_receipt_image_file(self):
        self._inverse_s3_image_file("receipt_image")

    def _ensure_user_tier(self):
        self.ensure_one()
        user = self.user_id
        user._update_tier()
        if user.tier_id:
            return user.tier_id

        base_tier = self.env["partner.tier"].search([
            ("partner_id", "=", self.partner_id.id),
        ], order="min_spending asc", limit=1)
        if base_tier:
            user.tier_id = base_tier
        return user.tier_id

    def _get_convert_points(self):
        self.ensure_one()
        tier = self._ensure_user_tier()
        if not tier or tier.convert_points <= 0:
            return 0
        return tier.convert_points

    def _calculate_reward_points(self, amount, convert_points):
        if amount <= 0 or convert_points <= 0:
            return 0
        return math.floor(amount / convert_points)

    def _refresh_reward_preview(self, save=False):
        for record in self.filtered(lambda item: item.state == "pending"):
            convert_points = record._get_convert_points()
            reward_points = record._calculate_reward_points(record.amount, convert_points)
            if save:
                if (
                    record.tier_convert_points != convert_points
                    or record.reward_points != reward_points
                ):
                    record.with_context(skip_reward_refresh=True).write({
                        "tier_convert_points": convert_points,
                        "reward_points": reward_points,
                    })
            else:
                record.tier_convert_points = convert_points
                record.reward_points = reward_points

    @api.onchange("amount", "user_id")
    def _onchange_amount_reward(self):
        self._refresh_reward_preview(save=False)

    def read(self, fields=None, load="_classic_read"):
        pending = self.filtered(lambda item: item.state == "pending" and item.amount > 0)
        if pending:
            pending._refresh_reward_preview(save=True)
        return super().read(fields, load)

    def write(self, vals):
        if self.env.context.get("skip_reward_refresh"):
            return super().write(vals)

        result = super().write(vals)
        if {"amount", "user_id"} & set(vals):
            self.filtered(lambda item: item.state == "pending")._refresh_reward_preview(
                save=True
            )
        return result

    @api.constrains("receipt_number", "partner_id", "state")
    def _check_unique_receipt_number(self):
        for record in self:
            if record.state == "rejected" or not record.receipt_number:
                continue

            duplicate = self.search([
                ("partner_id", "=", record.partner_id.id),
                ("receipt_number", "=", record.receipt_number),
                ("state", "!=", "rejected"),
                ("id", "!=", record.id),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    f"เลขที่ใบเสร็จ '{record.receipt_number}' ถูกใช้งานแล้ว"
                )

    @api.constrains("user_id", "partner_id")
    def _check_user_partner(self):
        for record in self:
            if record.user_id and record.partner_id and record.user_id.partner_id != record.partner_id:
                raise ValidationError("ผู้ใช้ต้องอยู่ภายใต้ Partner เดียวกัน")

    def _get_spending_currency(self):
        self.ensure_one()
        return self.partner_id.currency_ids.filtered("is_total_spending")[:1]

    def _get_default_currency(self):
        self.ensure_one()
        return self.partner_id.currency_ids.filtered("is_default")[:1]

    def action_approve(self):
        for record in self:
            record._approve()

    def action_reject(self):
        for record in self:
            record._reject()

    def _approve(self):
        self.ensure_one()
        if self.state != "pending":
            raise ValidationError("สามารถอนุมัติได้เฉพาะรายการที่รอตรวจสอบ")

        if self.amount <= 0:
            raise ValidationError("กรุณาระบุมูลค่าสินค้ามากกว่า 0")

        spending_currency = self._get_spending_currency()
        default_currency = self._get_default_currency()
        if not spending_currency:
            raise ValidationError("Partner ยังไม่ได้ตั้งค่า Total Spending currency")
        if not default_currency:
            raise ValidationError("Partner ยังไม่ได้ตั้งค่า Default currency")

        self._refresh_reward_preview(save=True)
        convert_points = self.tier_convert_points or self._get_convert_points()
        reward_value = self.reward_points or self._calculate_reward_points(
            self.amount,
            convert_points,
        )
        if convert_points <= 0:
            tier_name = self.user_id.tier_id.name if self.user_id.tier_id else "-"
            raise ValidationError(
                f"Tier '{tier_name}' ยังไม่ได้ตั้งค่า Convert Points "
                f"(Partner → Loyalty → Tiers)"
            )

        now = fields.Datetime.now()

        spending_point = self.env["crm.user.point"].create({
            "name": f"คะแนนจากใบเสร็จ {self.receipt_number}",
            "admin_note": f"Receipt #{self.receipt_number} approved",
            "value": self.amount,
            "type": "earn",
            "given_date": now,
            "currency_id": spending_currency.id,
            "user_id": self.user_id.id,
            "receipt_redeem_id": self.id,
        })

        reward_point = False
        if reward_value > 0:
            reward_point = self.env["crm.user.point"].create({
                "name": f"คะแนนจากใบเสร็จ {self.receipt_number}",
                "admin_note": (
                    f"Receipt #{self.receipt_number} "
                    f"({self.amount:g} / {convert_points:g} = {reward_value:g} points)"
                ),
                "value": reward_value,
                "type": "earn",
                "given_date": now,
                "currency_id": default_currency.id,
                "user_id": self.user_id.id,
                "receipt_redeem_id": self.id,
            })

        self.write({
            "state": "approved",
            "reviewed_date": now,
            "reviewed_by_id": self.env.user.id,
            "spending_point_id": spending_point.id,
            "reward_point_id": reward_point.id if reward_point else False,
            "reject_reason": False,
            "tier_convert_points": convert_points,
            "reward_points": reward_value,
        })

    def _reject(self):
        self.ensure_one()
        if self.state != "pending":
            raise ValidationError("สามารถปฏิเสธได้เฉพาะรายการที่รอตรวจสอบ")

        self.write({
            "state": "rejected",
            "reviewed_date": fields.Datetime.now(),
            "reviewed_by_id": self.env.user.id,
        })

    @api.model
    def _generate_manual_receipt_number(self, partner):
        slug = (partner.slug or "partner").strip()
        for _ in range(10):
            now = fields.Datetime.now()
            datetime_part = now.strftime("%Y%m%d_%H%M%S")
            random_part = f"{secrets.randbelow(10000):04d}"
            receipt_number = f"manual_receipt_{slug}_{datetime_part}_{random_part}"
            duplicate = self.search([
                ("partner_id", "=", partner.id),
                ("receipt_number", "=", receipt_number),
                ("state", "!=", "rejected"),
            ], limit=1)
            if not duplicate:
                return receipt_number

        raise ValidationError("ไม่สามารถสร้างเลขที่ใบเสร็จได้ กรุณาลองใหม่อีกครั้ง")

    @api.model
    def lookup_member(self, partner, query):
        query = (query or "").strip()
        if not query:
            raise ValidationError("กรุณาระบุข้อมูลสมาชิก")

        user_model = self.env["crm.user"].sudo()
        domain_base = [("partner_id", "=", partner.id)]
        for field_name in ("line_user_id", "phone", "email"):
            user = user_model.search(
                domain_base + [(field_name, "=", query)],
                limit=1,
            )
            if user:
                return user

        return False

    @api.model
    def submit_manual_receipt(self, partner, user, amount, image_data):
        amount = float(amount or 0)
        if amount <= 0:
            raise ValidationError("กรุณาระบุมูลค่าสินค้ามากกว่า 0")
        if partner.portal_manual_receipt_require_image and not image_data:
            raise ValidationError("กรุณาอัปโหลดรูปใบเสร็จ")

        receipt_number = self._generate_manual_receipt_number(partner)
        receipt_image_url = False
        if image_data:
            receipt_image_url = self._upload_image_field(
                "receipt_image",
                image_data,
            )

        return self.create({
            "receipt_number": receipt_number,
            "receipt_image": receipt_image_url,
            "partner_id": partner.id,
            "user_id": user.id,
            "amount": amount,
            "submitted_date": fields.Datetime.now(),
            "state": "pending",
        })

    @api.model
    def submit_receipt(self, partner, user, receipt_number, image_data):
        receipt_number = (receipt_number or "").strip()
        if not receipt_number:
            raise ValidationError("กรุณาระบุเลขที่ใบเสร็จ")
        if not image_data:
            raise ValidationError("กรุณาอัปโหลดรูปใบเสร็จ")

        duplicate = self.search([
            ("partner_id", "=", partner.id),
            ("receipt_number", "=", receipt_number),
            ("state", "!=", "rejected"),
        ], limit=1)
        if duplicate:
            raise ValidationError(f"เลขที่ใบเสร็จ '{receipt_number}' ถูกใช้งานแล้ว")

        receipt_image_url = self._upload_image_field(
            "receipt_image",
            image_data,
        )

        return self.create({
            "receipt_number": receipt_number,
            "receipt_image": receipt_image_url,
            "partner_id": partner.id,
            "user_id": user.id,
            "submitted_date": fields.Datetime.now(),
            "state": "pending",
        })
