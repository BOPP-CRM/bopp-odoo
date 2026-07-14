from odoo import api, fields, models


class PartnerZortoutWebhookLog(models.Model):
    _name = "partner.zortout.webhook.log"
    _description = "Partner Zortout Webhook Log"
    _order = "received_at desc, id desc"

    received_at = fields.Datetime(
        string="Received At",
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    webhook_method = fields.Char(string="Method", index=True)
    http_status = fields.Integer(string="HTTP Status", default=200)
    result_status = fields.Char(string="Result Status", index=True)
    message = fields.Text(string="Message")

    zortout_order_id = fields.Integer(string="Zortout Order ID", index=True)
    order_number = fields.Char(string="Order Number")
    amount = fields.Float(string="Amount")
    payment_status = fields.Char(string="Payment Status")
    order_status = fields.Char(string="Order Status")
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
    zortout_order_record_id = fields.Many2one(
        "partner.zortout.order",
        string="Zortout Order Record",
        ondelete="set null",
    )

    @api.model
    def _extract_payload_fields(self, payload):
        payload = payload or {}
        zortout_order_id = payload.get("id")
        try:
            zortout_order_id = int(zortout_order_id) if zortout_order_id is not None else False
        except (TypeError, ValueError):
            zortout_order_id = False

        amount = payload.get("amount")
        try:
            amount = float(amount or 0)
        except (TypeError, ValueError):
            amount = 0.0

        return {
            "zortout_order_id": zortout_order_id or False,
            "order_number": payload.get("number") or False,
            "amount": amount,
            "payment_status": payload.get("paymentstatus") or False,
            "order_status": payload.get("status") or False,
            "customer_name": payload.get("customername") or False,
            "customer_phone": payload.get("customerphone") or False,
            "customer_email": payload.get("customeremail") or False,
        }

    @api.model
    def log_request(
        self,
        partner,
        method,
        payload=None,
        http_status=200,
        result=None,
        message=None,
    ):
        result = result or {}
        payload_fields = self._extract_payload_fields(payload)

        order_record = self.env["partner.zortout.order"].browse()
        order_id = result.get("order_id")
        if order_id:
            order_record = self.env["partner.zortout.order"].sudo().browse(order_id).exists()

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

        if not message and result.get("reason"):
            message = result["reason"]
        if not message and warning == "member_not_found":
            message = "ไม่พบสมาชิกจากเบอร์โทรหรืออีเมลของออเดอร์"

        return self.sudo().create({
            "partner_id": partner.id,
            "webhook_method": (method or "").strip().upper() or False,
            "http_status": http_status,
            "result_status": result_status,
            "message": message or False,
            "warning": warning or False,
            "user_id": user.id if user else False,
            "zortout_order_record_id": order_record.id if order_record else False,
            **payload_fields,
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
        if self.points_awarded and not reward_points and self.zortout_order_record_id:
            reward_points = self.zortout_order_record_id.reward_points or 0

        return {
            "id": self.id,
            "received_at": fields.Datetime.to_string(self.received_at),
            "method": self.webhook_method or False,
            "http_status": self.http_status,
            "result_status": self.result_status or False,
            "message": self.message or False,
            "warning": self.warning or False,
            "zortout_order_id": self.zortout_order_id or False,
            "order_number": self.order_number or False,
            "amount": self.amount or 0,
            "payment_status": self.payment_status or False,
            "order_status": self.order_status or False,
            "customer_name": self.customer_name or False,
            "customer_phone": self.customer_phone or False,
            "customer_email": self.customer_email or False,
            "points_awarded": bool(self.points_awarded),
            "reward_points": reward_points,
            "member": member,
        }
