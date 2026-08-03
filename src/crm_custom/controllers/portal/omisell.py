from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalOmisellController(http.Controller):
    DEFAULT_LOG_LIMIT = 20
    MAX_LOG_LIMIT = 100

    @http.route(
        "/api/portal/omisell",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_omisell_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        return json_response({
            "omisell": partner.serialize_omisell_status(),
        })

    @http.route(
        "/api/portal/omisell/enable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def enable_omisell(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.enable_omisell_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "omisell": status,
            "message": "Omisell integration enabled.",
        }, status=201)

    @http.route(
        "/api/portal/omisell/disable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def disable_omisell(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        status = partner.disable_omisell_for_api()
        return json_response({
            "omisell": status,
        })

    @http.route(
        "/api/portal/omisell/regenerate-secret",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def regenerate_omisell_secret(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.regenerate_omisell_secret_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "omisell": status,
            "message": "Omisell webhook secret regenerated. Update it in Omisell webhook settings.",
        })

    @http.route(
        "/api/portal/omisell/logs",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_omisell_logs(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        limit = self._parse_limit(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        log_model = request.env["partner.omisell.webhook.log"].sudo()
        logs, total = log_model.search_for_portal(partner, limit=limit, offset=offset)

        return json_response({
            "logs": [log.serialize_for_portal() for log in logs],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_limit(self, value):
        parsed = self._parse_int(value)
        if parsed is None:
            return self.DEFAULT_LOG_LIMIT
        return min(max(parsed, 1), self.MAX_LOG_LIMIT)
