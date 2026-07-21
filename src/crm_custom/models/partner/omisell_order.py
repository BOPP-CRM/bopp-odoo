from odoo import api, fields, models


class PartnerOmisellOrder(models.Model):
    _name = "partner.omisell.order"
    _description = "Partner Omisell Order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "last_webhook_at desc, id desc"
    _sql_constraints = [
        (
            "partner_omisell_order_uniq",
            "unique(partner_id, omisell_order_number)",
            "Omisell order must be unique per partner.",
        ),
    ]

    omisell_order_number = fields.Char(string="Omisell Order Number", required=True, index=True)
    order_number = fields.Char(string="Order Number", tracking=True)
    amount = fields.Float(string="Amount", tracking=True)
    payment_status = fields.Char(string="Payment Status", tracking=True)
    payment_method = fields.Char(string="Payment Method", tracking=True)
    order_status_id = fields.Integer(string="Order Status ID", tracking=True)
    order_status_name = fields.Char(string="Order Status", tracking=True)
    is_fulfilled = fields.Boolean(string="Is Fulfilled", tracking=True)
    customer_name = fields.Char(string="Customer Name")
    customer_phone = fields.Char(string="Customer Phone")
    customer_email = fields.Char(string="Customer Email")
    webhook_event = fields.Char(string="Last Webhook Event", tracking=True)
    webhook_request_id = fields.Char(string="Request ID", index=True)
    platform = fields.Char(string="Platform")
    shop_id = fields.Integer(string="Shop ID")
    seller_id = fields.Char(string="Seller ID")
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
    tier_convert_points = fields.Float(string="Tier Convert Points", readonly=True)
    reward_points = fields.Float(string="Reward Points", readonly=True)

    @api.model
    def process_webhook(self, partner, payload):
        payload = payload or {}
        event = (payload.get("event") or "").strip()
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        omisell_order_number = (payload_data.get("omisell_order_number") or "").strip()
        if not omisell_order_number:
            return {"status": "ignored", "reason": "missing_omisell_order_number"}

        order = self.search([
            ("partner_id", "=", partner.id),
            ("omisell_order_number", "=", omisell_order_number),
        ], limit=1)

        vals = self._prepare_order_vals(partner, payload, order_detail=None)
        if order:
            order.write(vals)
        else:
            order = self.create(vals)

        try:
            order_detail = partner.fetch_omisell_order_detail(omisell_order_number)
        except Exception as error:
            order.write({"error_message": str(error)})
            return {
                "status": "ok",
                "order_id": order.id,
                "points_awarded": False,
                "warning": "detail_fetch_failed",
                "message": str(error),
                "event": event or False,
            }

        order.write(self._prepare_order_vals(partner, payload, order_detail=order_detail))

        if order.points_awarded:
            return {"status": "ok", "order_id": order.id, "points_awarded": False}

        if not partner.is_omisell_order_eligible_for_points(payload, order_detail):
            return {"status": "ok", "order_id": order.id, "points_awarded": False}

        user = partner.find_user_from_omisell_order_detail(order_detail)
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

    def _prepare_order_vals(self, partner, payload, order_detail=None):
        payload = payload or {}
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        detail = order_detail if isinstance(order_detail, dict) else {}
        receiver = detail.get("receiver") if isinstance(detail.get("receiver"), dict) else {}

        order_status_id = partner._parse_omisell_status_id(
            detail.get("status_id") if detail else payload_data.get("status_id")
        )
        order_status_name = detail.get("status_name") or payload_data.get("status_name") or False
        payment_status = partner.get_omisell_payment_status(detail)
        payment_method = partner.get_omisell_payment_method(detail)

        return {
            "partner_id": partner.id,
            "omisell_order_number": (
                detail.get("omisell_order_number")
                or payload_data.get("omisell_order_number")
                or False
            ),
            "order_number": detail.get("order_number") or payload_data.get("order_number") or False,
            "amount": partner.get_omisell_order_amount(detail),
            "payment_status": payment_status or False,
            "payment_method": payment_method or False,
            "order_status_id": order_status_id or False,
            "order_status_name": order_status_name,
            "is_fulfilled": bool(
                detail.get("is_fulfilled")
                if detail
                else payload_data.get("is_fulfilled")
            ),
            "customer_name": receiver.get("fullname") or False,
            "customer_phone": receiver.get("phone") or False,
            "customer_email": receiver.get("email") or False,
            "webhook_event": payload.get("event") or False,
            "webhook_request_id": payload.get("request_id") or False,
            "platform": detail.get("platform") or payload.get("platform") or False,
            "shop_id": detail.get("shop_id") or payload.get("shop_id") or False,
            "seller_id": str(detail.get("seller_id") or payload.get("seller_id") or "").strip() or False,
            "last_webhook_at": fields.Datetime.now(),
            "error_message": False,
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
        order_label = self.order_number or self.omisell_order_number

        spending_point = self.env["crm.user.point"].create({
            "name": f"คะแนนจาก {order_label}",
            "admin_note": f"Order #{order_label} approved",
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

        self.write({
            "user_id": user.id,
            "points_awarded": True,
            "points_awarded_at": now,
            "spending_point_id": spending_point.id,
            "reward_point_id": reward_point.id if reward_point else False,
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
        }
