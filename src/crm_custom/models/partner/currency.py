from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerPointCurrency(models.Model):
    _name = "crm.partner.currency"
    _description = "Currency"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True)
    is_default = fields.Boolean(default=False)
    is_total_spending = fields.Boolean(default=False)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("is_default") and vals.get("partner_id"):
                self._unset_other_default_currency(vals["partner_id"])
            if vals.get("is_total_spending") and vals.get("partner_id"):
                self._unset_other_total_spending_currency(vals["partner_id"])

        return super().create(vals_list)

    def write(self, vals):
        if vals.get("is_default"):
            partner_ids = vals.get("partner_id") or self.mapped("partner_id").ids
            if isinstance(partner_ids, int):
                partner_ids = [partner_ids]

            for partner_id in partner_ids:
                self._unset_other_default_currency(partner_id)

        if vals.get("is_total_spending"):
            partner_ids = vals.get("partner_id") or self.mapped("partner_id").ids
            if isinstance(partner_ids, int):
                partner_ids = [partner_ids]

            for partner_id in partner_ids:
                self._unset_other_total_spending_currency(partner_id)

        return super().write(vals)

    @api.model
    def _unset_other_default_currency(self, partner_id):
        self.search([
            ("partner_id", "=", partner_id),
            ("is_default", "=", True),
        ]).write({"is_default": False})

    @api.model
    def _unset_other_total_spending_currency(self, partner_id):
        self.search([
            ("partner_id", "=", partner_id),
            ("is_total_spending", "=", True),
        ]).write({"is_total_spending": False})

    @api.constrains("is_default", "partner_id")
    def _check_single_default_currency(self):
        for record in self:
            if not record.is_default or not record.partner_id:
                continue

            default_count = self.search_count([
                ("partner_id", "=", record.partner_id.id),
                ("is_default", "=", True),
                ("id", "!=", record.id),
            ])
            if default_count:
                raise ValidationError("สามารถตั้งค่า Default ได้เพียง 1 Currency เท่านั้น")

    @api.constrains("is_total_spending", "partner_id")
    def _check_single_total_spending_currency(self):
        for record in self:
            if not record.is_total_spending or not record.partner_id:
                continue

            total_spending_count = self.search_count([
                ("partner_id", "=", record.partner_id.id),
                ("is_total_spending", "=", True),
                ("id", "!=", record.id),
            ])
            if total_spending_count:
                raise ValidationError("สามารถตั้งค่า Total Spending ได้เพียง 1 Currency เท่านั้น")
