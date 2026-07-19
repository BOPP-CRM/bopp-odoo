from odoo import api, fields, models


class PartnerZortoutOrder(models.Model):
    _name = "partner.zortout.order"
    _description = "Partner Zortout Order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "last_webhook_at desc, id desc"
    _sql_constraints = [
        (
            "partner_zortout_order_uniq",
            "unique(partner_id, zortout_order_id)",
            "Zortout order must be unique per partner.",
        ),
    ]

    zortout_order_id = fields.Integer(string="Zortout Order ID", required=True, index=True)
    order_number = fields.Char(string="Order Number", tracking=True)
    amount = fields.Float(string="Amount", tracking=True)
    payment_status = fields.Char(string="Payment Status", tracking=True)
    order_status = fields.Char(string="Order Status", tracking=True)
    payment_amount = fields.Float(string="Payment Amount")
    customer_name = fields.Char(string="Customer Name")
    customer_phone = fields.Char(string="Customer Phone")
    customer_email = fields.Char(string="Customer Email")
    webhook_method = fields.Char(string="Last Webhook Method", tracking=True)
    last_webhook_at = fields.Datetime(string="Last Webhook At", tracking=True)
    points_awarded = fields.Boolean(string="Points Awarded", default=False, tracking=True)
    points_awarded_at = fields.Datetime(string="Points Awarded At", readonly=True)
    error_message = fields.Text(string="Error Message", tracking=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    user_id = fields.Many2one(
        "crm.user",
        string="Member",
        ondelete="set null",
        tracking=True,
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
    receipt_redeem_id = fields.Many2one(
        "crm.partner.receipt.redeem",
        string="Receipt Redeem",
        readonly=True,
        ondelete="set null",
    )
    tier_convert_points = fields.Float(string="Tier Convert Points", readonly=True)
    reward_points = fields.Float(string="Reward Points", readonly=True)

    @api.model
    def process_webhook(self, partner, method, payload):
        method = (method or "").strip().upper()
        if method == "DELETEORDER":
            return self._process_delete_order(partner, payload)

        if method not in {"ADDORDER", "UPDATEORDER"}:
            return {"status": "ignored", "reason": "unsupported_method"}

        zortout_order_id = payload.get("id")
        if not zortout_order_id:
            return {"status": "ignored", "reason": "missing_order_id"}

        order = self.search([
            ("partner_id", "=", partner.id),
            ("zortout_order_id", "=", int(zortout_order_id)),
        ], limit=1)

        vals = {
            "partner_id": partner.id,
            "zortout_order_id": int(zortout_order_id),
            "order_number": payload.get("number") or False,
            "amount": partner._parse_zortout_amount(payload.get("amount")),
            "payment_amount": partner._parse_zortout_amount(payload.get("paymentamount")),
            "payment_status": payload.get("paymentstatus") or False,
            "order_status": payload.get("status") or False,
            "customer_name": payload.get("customername") or False,
            "customer_phone": payload.get("customerphone") or False,
            "customer_email": payload.get("customeremail") or False,
            "webhook_method": method,
            "last_webhook_at": fields.Datetime.now(),
            "error_message": False,
        }

        if order:
            order.write(vals)
        else:
            order = self.create(vals)

        if order._should_revoke_points(partner, payload):
            return order._revoke_points()

        if order.points_awarded:
            return {"status": "ok", "order_id": order.id, "points_awarded": False}

        if not partner.is_zortout_payment_successful(payload):
            return {"status": "ok", "order_id": order.id, "points_awarded": False}

        user = partner.find_user_from_zortout_payload(payload)
        if not user:
            order.write({
                "error_message": "ไม่พบสมาชิกจากเบอร์โทรหรืออีเมลของออเดอร์",
            })
            return {
                "status": "ok",
                "order_id": order.id,
                "points_awarded": False,
                "warning": "member_not_found",
            }

        return order._award_points(user, partner)

    @api.model
    def _process_delete_order(self, partner, payload):
        zortout_order_id = payload.get("id")
        if not zortout_order_id:
            return {"status": "ignored", "reason": "missing_order_id"}

        order = self.search([
            ("partner_id", "=", partner.id),
            ("zortout_order_id", "=", int(zortout_order_id)),
        ], limit=1)
        if not order:
            return {
                "status": "ok",
                "points_revoked": False,
                "reason": "order_not_found",
            }

        order.write({
            "order_number": payload.get("number") or order.order_number,
            "webhook_method": "DELETEORDER",
            "last_webhook_at": fields.Datetime.now(),
            "order_status": "Deleted",
            "error_message": False,
        })
        result = order._revoke_points()
        result["order_id"] = order.id
        return result

    def _should_revoke_points(self, partner, payload):
        self.ensure_one()
        if partner.is_zortout_order_voided(payload):
            return True
        if self.points_awarded and not partner.is_zortout_payment_successful(payload):
            return True
        return False

    def _get_receipt_number(self):
        self.ensure_one()
        if self.order_number:
            return self.order_number
        return f"zortout-{self.zortout_order_id}"

    def _revoke_points(self):
        self.ensure_one()
        if not self.points_awarded:
            return {"status": "ok", "points_revoked": False}

        now = fields.Datetime.now()
        points_to_delete = (self.spending_point_id | self.reward_point_id).exists()

        receipt = self.receipt_redeem_id
        if receipt and receipt.state == "approved":
            receipt.write({
                "state": "rejected",
                "reject_reason": "Zortout order voided or deleted",
                "reviewed_date": now,
                "spending_point_id": False,
                "reward_point_id": False,
            })

        if points_to_delete:
            points_to_delete.unlink()

        self.write({
            "points_awarded": False,
            "points_awarded_at": False,
            "spending_point_id": False,
            "reward_point_id": False,
            "tier_convert_points": 0,
            "reward_points": 0,
            "error_message": False,
        })

        return {
            "status": "ok",
            "order_id": self.id,
            "points_revoked": True,
        }

    def _award_points(self, user, partner):
        self.ensure_one()
        if self.amount <= 0:
            self.write({
                "user_id": user.id,
                "error_message": "มูลค่าออเดอร์ต้องมากกว่า 0",
            })
            return {
                "status": "ok",
                "order_id": self.id,
                "points_awarded": False,
                "warning": "invalid_amount",
            }

        spending_currency = partner._get_spending_currency()
        default_currency = partner._get_default_point_currency()
        if not spending_currency or not default_currency:
            self.write({
                "user_id": user.id,
                "error_message": "Partner ยังไม่ได้ตั้งค่า currency สำหรับคะแนน",
            })
            return {
                "status": "ok",
                "order_id": self.id,
                "points_awarded": False,
                "warning": "missing_currency",
            }

        convert_points = partner._get_user_convert_points(user)
        reward_value = partner._calculate_reward_points(self.amount, convert_points)
        if convert_points <= 0:
            tier_name = user.tier_id.name if user.tier_id else "-"
            self.write({
                "user_id": user.id,
                "error_message": f"Tier '{tier_name}' ยังไม่ได้ตั้งค่า Convert Points",
            })
            return {
                "status": "ok",
                "order_id": self.id,
                "points_awarded": False,
                "warning": "missing_convert_points",
            }

        now = fields.Datetime.now()
        order_label = self.order_number or str(self.zortout_order_id)

        spending_point = self.env["crm.user.point"].create({
            "name": f"คะแนนจาก {order_label}",
            "admin_note": f"Order #{order_label} paid",
            "value": self.amount,
            "type": "earn",
            "given_date": now,
            "currency_id": spending_currency.id,
            "user_id": user.id,
        })

        reward_point = False
        if reward_value > 0:
            reward_point = self.env["crm.user.point"].create({
                "name": f"คะแนนจาก {order_label}",
                "admin_note": (
                    f"Order #{order_label} "
                    f"({self.amount:g} / {convert_points:g} = {reward_value:g} points)"
                ),
                "value": reward_value,
                "type": "earn",
                "given_date": now,
                "currency_id": default_currency.id,
                "user_id": user.id,
            })

        receipt = self.env["crm.partner.receipt.redeem"].create({
            "receipt_number": self._get_receipt_number(),
            "partner_id": partner.id,
            "user_id": user.id,
            "amount": self.amount,
            "submitted_date": now,
            "state": "approved",
            "reviewed_date": now,
            "spending_point_id": spending_point.id,
            "reward_point_id": reward_point.id if reward_point else False,
            "tier_convert_points": convert_points,
            "reward_points": reward_value,
            "zortout_order_id": self.id,
        })

        spending_point.receipt_redeem_id = receipt.id
        if reward_point:
            reward_point.receipt_redeem_id = receipt.id

        self.write({
            "user_id": user.id,
            "points_awarded": True,
            "points_awarded_at": now,
            "spending_point_id": spending_point.id,
            "reward_point_id": reward_point.id if reward_point else False,
            "receipt_redeem_id": receipt.id,
            "tier_convert_points": convert_points,
            "reward_points": reward_value,
            "error_message": False,
        })

        return {
            "status": "ok",
            "order_id": self.id,
            "points_awarded": True,
            "user_id": user.id,
            "reward_points": reward_value,
            "receipt_redeem_id": receipt.id,
        }
