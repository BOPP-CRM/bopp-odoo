from odoo import fields, models


class PartnerBroadcastRecipient(models.Model):
    _name = "partner.broadcast.recipient"
    _description = "Partner Broadcast Recipient"
    _order = "id"
    _sql_constraints = [
        (
            "broadcast_user_uniq",
            "unique(broadcast_id, user_id)",
            "A member can only appear once per broadcast.",
        ),
    ]

    broadcast_id = fields.Many2one(
        "partner.broadcast",
        string="Broadcast",
        required=True,
        ondelete="cascade",
        index=True,
    )
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
        required=True,
        ondelete="cascade",
    )
    line_user_id = fields.Char(string="LINE UUID")

    coupon_status = fields.Selection(
        [
            ("skipped", "Skipped"),
            ("pending", "Pending"),
            ("granted", "Granted"),
            ("pool_empty", "Pool Empty"),
            ("error", "Error"),
        ],
        string="Coupon Status",
        default="skipped",
        required=True,
    )
    user_coupon_id = fields.Many2one(
        "crm.user.coupon",
        string="Granted Coupon",
        ondelete="set null",
        readonly=True,
    )
    line_status = fields.Selection(
        [
            ("pending", "Pending"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        string="LINE Status",
        default="pending",
        required=True,
    )
    line_error = fields.Text(string="Error")

    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "user_id": self.user_id.id,
            "display_name": self.user_id.display_name,
            "line_user_id": self.line_user_id or False,
            "coupon_status": self.coupon_status,
            "coupon_code": self.user_coupon_id.code if self.user_coupon_id else False,
            "line_status": self.line_status,
            "line_error": self.line_error or False,
        }
