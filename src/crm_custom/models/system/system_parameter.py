from odoo import api, fields, models
from odoo.exceptions import ValidationError


class CrmSystemParameter(models.Model):
    _name = "crm.system.parameter"
    _description = "CRM System Parameter"
    _order = "key"

    key = fields.Char(string="Key", required=True, index=True)
    value = fields.Char(string="Value", required=True)
    description = fields.Text(string="Description")

    _sql_constraints = [
        (
            "crm_system_parameter_key_uniq",
            "unique(key)",
            "System parameter key must be unique.",
        ),
    ]

    @api.constrains("key")
    def _check_key(self):
        for record in self:
            if not (record.key or "").strip():
                raise ValidationError("Key is required.")

    @api.model
    def get_value(self, key, default=None):
        record = self.search([("key", "=", key)], limit=1)
        if not record:
            return default
        return record.value if record.value is not None else default

    @api.model
    def get_int(self, key, default=0):
        raw = self.get_value(key, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
