from datetime import datetime, timedelta

from odoo import api, fields, models

THAILAND_OFFSET = timedelta(hours=7)


class PartnerPortalApiUsage(models.Model):
    _name = "partner.portal.api.usage"
    _description = "Partner Portal API Key Usage"
    _order = "month desc, partner_id"

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    month = fields.Char(string="Month", required=True, index=True)
    request_count = fields.Integer(string="Request Count", default=0)

    _sql_constraints = [
        (
            "partner_month_uniq",
            "unique(partner_id, month)",
            "Usage record already exists for this partner and month.",
        ),
    ]

    @api.model
    def _current_month_key(self):
        now = datetime.utcnow() + THAILAND_OFFSET
        return now.strftime("%Y-%m")

    @api.model
    def _get_month_usage_record(self, partner, month=None):
        if not partner:
            return self.browse()

        return self.sudo().search([
            ("partner_id", "=", partner.id),
            ("month", "=", month or self._current_month_key()),
        ], limit=1)

    @api.model
    def _serialize_month_usage(self, partner, month, used):
        limit = partner.api_monthly_limit or 0
        remaining = None if limit <= 0 else max(limit - used, 0)

        return {
            "month": month,
            "limit": limit if limit > 0 else None,
            "used": used,
            "remaining": remaining,
            "unlimited": limit <= 0,
        }

    @api.model
    def get_current_month_count(self, partner):
        usage = self._get_month_usage_record(partner)
        return usage.request_count if usage else 0

    @api.model
    def get_usage_summary(self, partner):
        month = self._current_month_key()
        usage = self._get_month_usage_record(partner, month)
        used = usage.request_count if usage else 0
        return self._serialize_month_usage(partner, month, used)

    @api.model
    def get_usage_history(self, partner, limit=12):
        if not partner:
            return []

        records = self.sudo().search(
            [("partner_id", "=", partner.id)],
            order="month desc",
            limit=limit or None,
        )
        return [
            self._serialize_month_usage(partner, record.month, record.request_count)
            for record in records
        ]

    @api.model
    def try_consume(self, partner):
        limit = partner.api_monthly_limit or 0
        month = self._current_month_key()
        usage = self._get_month_usage_record(partner, month)

        if limit > 0:
            current_count = usage.request_count if usage else 0
            if current_count >= limit:
                return False, self.get_usage_summary(partner)

        if usage:
            usage.write({"request_count": usage.request_count + 1})
        else:
            usage = self.sudo().create({
                "partner_id": partner.id,
                "month": month,
                "request_count": 1,
            })

        summary = self.get_usage_summary(partner)
        summary["used"] = usage.request_count
        if summary["limit"]:
            summary["remaining"] = max(summary["limit"] - summary["used"], 0)

        return True, summary
