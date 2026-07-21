from odoo import api, fields, models


class PartnerOmisellWebhookLog(models.Model):
    _name = "partner.omisell.webhook.log"
    _description = "Partner Omisell Webhook Log"
    _order = "received_at desc, id desc"

    received_at = fields.Datetime(
        string="Received At",
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    webhook_event = fields.Char(string="Event", index=True)
    request_id = fields.Char(string="Request ID", index=True)
    http_status = fields.Integer(string="HTTP Status", default=200)
    result_status = fields.Char(string="Result Status", index=True)
    message = fields.Text(string="Message")
    platform = fields.Char(string="Platform")
    shop_id = fields.Integer(string="Shop ID")
    seller_id = fields.Char(string="Seller ID")

    omisell_order_number = fields.Char(string="Omisell Order Number", index=True)
    order_number = fields.Char(string="Order Number")
    amount = fields.Float(string="Amount")
    payment_status = fields.Char(string="Payment Status")
    order_status_id = fields.Integer(string="Order Status ID")
    order_status_name = fields.Char(string="Order Status")
    customer_name = fields.Char(string="Customer Name")
    customer_phone = fields.Char(string="Customer Phone")
    customer_email = fields.Char(string="Customer Email")
    points_awarded = fields.Boolean(string="Points Awarded", default=False)
    reward_points = fields.Float(string="Reward Points")
    warning = fields.Char(string="Warning")

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
    )
    omisell_order_record_id = fields.Many2one(
        "partner.omisell.order",
        string="Omisell Order Record",
        ondelete="set null",
    )

    @api.model
    def _extract_payload_fields(self, payload):
        payload = payload or {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

        amount = 0.0
        order_status_id = data.get("status_id")
        try:
            order_status_id = int(order_status_id) if order_status_id is not None else False
        except (TypeError, ValueError):
            order_status_id = False

        return {
            "webhook_event": payload.get("event") or False,
            "request_id": payload.get("request_id") or False,
            "platform": payload.get("platform") or False,
            "shop_id": payload.get("shop_id") or False,
            "seller_id": str(payload.get("seller_id") or "").strip() or False,
            "omisell_order_number": data.get("omisell_order_number") or False,
            "order_number": data.get("order_number") or False,
            "amount": amount,
            "order_status_id": order_status_id or False,
            "order_status_name": data.get("status_name") or False,
        }

    @api.model
    def log_request(
        self,
        partner,
        payload=None,
        http_status=200,
        result=None,
        message=None,
    ):
        result = result or {}
        payload_fields = self._extract_payload_fields(payload)

        order_record = self.env["partner.omisell.order"].browse()
        order_id = result.get("order_id")
        if order_id:
            order_record = self.env["partner.omisell.order"].sudo().browse(order_id).exists()

        user = order_record.user_id if order_record else self.env["crm.user"]
        if not user and result.get("user_id"):
            user = self.env["crm.user"].sudo().browse(result["user_id"]).exists()

        warning = result.get("warning") or False
        result_status = result.get("status") or ("error" if http_status >= 400 else "ok")
        points_awarded = bool(result.get("points_awarded"))
        reward_points = result.get("reward_points")
        if reward_points is None and order_record:
            reward_points = order_record.reward_points
        try:
            reward_points = float(reward_points or 0)
        except (TypeError, ValueError):
            reward_points = 0.0

        if not message and result.get("message"):
            message = result["message"]
        if not message and result.get("reason"):
            message = result["reason"]
        if not message and warning == "member_not_found":
            message = "ไม่พบสมาชิกจากเบอร์โทรหรืออีเมลของออเดอร์"

        order_fields = {}
        if order_record:
            order_fields = {
                "omisell_order_number": order_record.omisell_order_number or payload_fields.get("omisell_order_number"),
                "order_number": order_record.order_number or payload_fields.get("order_number"),
                "amount": order_record.amount,
                "payment_status": order_record.payment_status or False,
                "order_status_id": order_record.order_status_id or payload_fields.get("order_status_id"),
                "order_status_name": order_record.order_status_name or payload_fields.get("order_status_name"),
                "customer_name": order_record.customer_name or False,
                "customer_phone": order_record.customer_phone or False,
                "customer_email": order_record.customer_email or False,
                "platform": order_record.platform or payload_fields.get("platform"),
                "shop_id": order_record.shop_id or payload_fields.get("shop_id"),
                "seller_id": order_record.seller_id or payload_fields.get("seller_id"),
            }

        return self.sudo().create({
            "partner_id": partner.id,
            "http_status": http_status,
            "result_status": result_status,
            "message": message or False,
            "warning": warning or False,
            "user_id": user.id if user else False,
            "omisell_order_record_id": order_record.id if order_record else False,
            **payload_fields,
            **order_fields,
            "points_awarded": points_awarded,
            "reward_points": reward_points,
        })

    @api.model
    def search_for_portal(self, partner, limit=20, offset=0):
        domain = [("partner_id", "=", partner.id)]
        total = self.sudo().search_count(domain)
        logs = self.sudo().search(
            domain,
            limit=limit,
            offset=offset,
            order="received_at desc, id desc",
        )
        return logs, total

    def serialize_for_portal(self):
        self.ensure_one()
        member = False
        if self.user_id:
            member = {
                "id": self.user_id.id,
                "display_name": self.user_id.display_name,
                "phone": self.user_id.phone or False,
                "email": self.user_id.email or False,
            }

        reward_points = self.reward_points or 0
        if self.points_awarded and not reward_points and self.omisell_order_record_id:
            reward_points = self.omisell_order_record_id.reward_points or 0

        return {
            "id": self.id,
            "received_at": fields.Datetime.to_string(self.received_at),
            "event": self.webhook_event or False,
            "request_id": self.request_id or False,
            "http_status": self.http_status,
            "result_status": self.result_status or False,
            "message": self.message or False,
            "warning": self.warning or False,
            "platform": self.platform or False,
            "shop_id": self.shop_id or False,
            "seller_id": self.seller_id or False,
            "omisell_order_number": self.omisell_order_number or False,
            "order_number": self.order_number or False,
            "amount": self.amount or 0,
            "payment_status": self.payment_status or False,
            "order_status_id": self.order_status_id or False,
            "order_status_name": self.order_status_name or False,
            "customer_name": self.customer_name or False,
            "customer_phone": self.customer_phone or False,
            "customer_email": self.customer_email or False,
            "points_awarded": bool(self.points_awarded),
            "reward_points": reward_points,
            "member": member,
        }
