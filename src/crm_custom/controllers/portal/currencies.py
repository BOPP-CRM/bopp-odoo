from odoo import http
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalCurrenciesController(http.Controller):
    @http.route("/api/portal/currencies", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_currencies(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        currencies = request.env["crm.partner.currency"].sudo().search([
            ("partner_id", "=", user.crm_partner_id.id),
        ], order="is_default desc, is_total_spending desc, id asc")

        return json_response({
            "currencies": [
                self._serialize_currency(currency)
                for currency in currencies
            ],
        })

    @http.route("/api/portal/currencies/<int:currency_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_currency(self, currency_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        currency_response = self._get_currency(user.crm_partner_id, currency_id)
        if currency_response["error"]:
            return currency_response["error"]

        return json_response({
            "currency": self._serialize_currency(currency_response["currency"]),
        })

    def _get_currency(self, partner, currency_id):
        currency = request.env["crm.partner.currency"].sudo().search([
            ("id", "=", currency_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not currency:
            return {
                "currency": False,
                "error": json_response(
                    {"error": "currency_not_found", "message": "ไม่พบ Currency ดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "currency": currency,
            "error": False,
        }

    def _serialize_currency(self, currency):
        return {
            "id": currency.id,
            "name": currency.name,
            "is_default": currency.is_default,
            "is_total_spending": currency.is_total_spending,
        }
