import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalOpenaiController(http.Controller):
    @http.route(
        "/api/portal/openai",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_openai_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        return json_response({
            "openai": partner.serialize_openai_status(),
        })

    @http.route(
        "/api/portal/openai",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def save_openai_api_key(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.save_openai_api_key_for_api(
                payload.get("api_key") or payload.get("apiKey"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "openai": status,
            "message": "บันทึก OpenAI API Key สำเร็จ",
        }, status=201)

    @http.route(
        "/api/portal/openai/remove",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def remove_openai_api_key(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        status = partner.remove_openai_api_key_for_api()
        return json_response({
            "openai": status,
            "message": "ลบ OpenAI API Key แล้ว",
        })

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}
