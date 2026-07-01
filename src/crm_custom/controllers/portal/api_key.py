from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_authenticated_portal_user
from ....util.request import json_response


class PortalApiKeyController(http.Controller):
    @http.route(
        "/api/portal/api-key",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_api_key_status(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        return json_response({
            "api_key": portal_user.sudo().serialize_portal_api_key_status(),
        })

    @http.route(
        "/api/portal/api-key/generate",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def generate_api_key(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        try:
            plain_key = portal_user.sudo().generate_portal_api_key_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "api_key_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "api_key": {
                "key": plain_key,
                **portal_user.sudo().serialize_portal_api_key_status(),
            },
            "message": "Save this API key now. It will not be shown again.",
        }, status=201)

    @http.route(
        "/api/portal/api-key/rotate",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def rotate_api_key(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        try:
            plain_key = portal_user.sudo().rotate_portal_api_key_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "api_key_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "api_key": {
                "key": plain_key,
                **portal_user.sudo().serialize_portal_api_key_status(),
            },
            "message": "Save this API key now. It will not be shown again.",
        })

    @http.route(
        "/api/portal/api-key/enable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def enable_api_key(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        try:
            portal_user.sudo().set_portal_api_key_enabled_for_api(True)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "api_key_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "api_key": portal_user.sudo().serialize_portal_api_key_status(),
        })

    @http.route(
        "/api/portal/api-key/disable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def disable_api_key(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        try:
            portal_user.sudo().set_portal_api_key_enabled_for_api(False)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "api_key_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "api_key": portal_user.sudo().serialize_portal_api_key_status(),
        })

    @http.route(
        "/api/portal/api-key/usage",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_api_key_usage(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        usage_model = request.env["partner.portal.api.usage"].sudo()
        limit = self._parse_int(kwargs.get("limit")) or 12
        usage = usage_model.get_usage_summary(partner)
        history = usage_model.get_usage_history(partner, limit=limit)

        return json_response({
            "usage": usage,
            "history": history,
            "partner": {
                "id": partner.id,
                "name": partner.name,
                "slug": partner.slug,
            },
        })

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
