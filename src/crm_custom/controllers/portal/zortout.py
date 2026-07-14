from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalZortoutController(http.Controller):
    DEFAULT_LOG_LIMIT = 20
    MAX_LOG_LIMIT = 100
    @http.route(
        "/api/portal/zortout",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_zortout_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        return json_response({
            "zortout": partner.serialize_zortout_status(),
        })

    @http.route(
        "/api/portal/zortout/enable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def enable_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.enable_zortout_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout integration enabled.",
        }, status=201)

    @http.route(
        "/api/portal/zortout/disable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def disable_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        status = partner.disable_zortout_for_api()
        return json_response({
            "zortout": status,
        })

    @http.route(
        "/api/portal/zortout/regenerate-keys",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def regenerate_zortout_keys(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.regenerate_zortout_keys_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout keys regenerated. Update them in ZORT portal.",
        })

    @http.route(
        "/api/portal/zortout/logs",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_zortout_logs(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        limit = self._parse_limit(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        log_model = request.env["partner.zortout.webhook.log"].sudo()
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
