import json

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
            "message": "Zortout keys regenerated and webhook synced.",
        })

    @http.route(
        "/api/portal/zortout/connect",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def connect_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.connect_zortout_for_api(
                payload.get("storename"),
                payload.get("apikey"),
                payload.get("apisecret"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout webhook configured successfully.",
        }, status=201)

    @http.route(
        "/api/portal/zortout/sync-webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def sync_zortout_webhook(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.resync_zortout_webhook_for_api(
                storename=payload.get("storename"),
                apikey=payload.get("apikey"),
                apisecret=payload.get("apisecret"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout webhook synced successfully.",
        })

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

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
