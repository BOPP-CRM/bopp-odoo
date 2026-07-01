from odoo import api, fields, models
from odoo.exceptions import ValidationError


DEFAULT_STATUSES = [
    {"code": "pending", "label": "รอตรวจสอบ", "sequence": 10, "is_default": True, "color": "#FFC107"},
    {"code": "approved", "label": "อนุมัติ", "sequence": 20, "is_default": False, "color": "#28A745"},
    {"code": "rejected", "label": "ปฏิเสธ", "sequence": 30, "is_default": False, "color": "#DC3545"},
]


class PartnerWarrantyStatus(models.Model):
    _name = "partner.warranty.status"
    _description = "Partner Warranty Status"
    _order = "sequence asc, id asc"

    code = fields.Char(string="Code", required=True)
    label = fields.Char(string="Label", required=True)
    sequence = fields.Integer(string="Sequence", default=10)
    color = fields.Char(string="Color")
    is_default = fields.Boolean(string="Default Status", default=False)
    active = fields.Boolean(string="Active", default=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    _sql_constraints = [
        (
            "partner_warranty_status_code_uniq",
            "unique(partner_id, code)",
            "Status code must be unique per partner.",
        ),
    ]

    @api.constrains("is_default", "partner_id")
    def _check_single_default(self):
        for record in self.filtered("is_default"):
            duplicate = self.search([
                ("partner_id", "=", record.partner_id.id),
                ("is_default", "=", True),
                ("id", "!=", record.id),
            ], limit=1)
            if duplicate:
                raise ValidationError("สามารถตั้งค่า Default Status ได้เพียง 1 รายการ")

    @api.model
    def ensure_default_statuses(self, partner):
        existing = self.with_context(active_test=False).search([
            ("partner_id", "=", partner.id),
        ], limit=1)
        if existing:
            return self.with_context(active_test=False).search([
                ("partner_id", "=", partner.id),
            ])

        created = self.create([
            {
                **status,
                "partner_id": partner.id,
            }
            for status in DEFAULT_STATUSES
        ])
        return created

    @api.model
    def get_default_status(self, partner):
        self.ensure_default_statuses(partner)
        status = self.search([
            ("partner_id", "=", partner.id),
            ("is_default", "=", True),
            ("active", "=", True),
        ], limit=1)
        if not status:
            status = self.search([
                ("partner_id", "=", partner.id),
                ("active", "=", True),
            ], order="sequence asc, id asc", limit=1)
        return status
