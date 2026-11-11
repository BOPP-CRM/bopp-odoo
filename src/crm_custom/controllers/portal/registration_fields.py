import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

ROUTE = "/api/portal/registration-fields"


class PortalRegistrationFieldsController(http.Controller):
    @http.route(ROUTE, type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_registration_fields(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        payload = request.env["partner.registration.field"].get_registration_form_for_partner(
            partner,
            include_hidden=True,
        )
        return json_response(payload)

    @http.route(ROUTE, type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def put_registration_fields(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        fields_payload = payload.get("fields")
        if fields_payload is None:
            return json_response(
                {"error": "invalid_request", "message": "fields is required."},
                status=400,
            )

        try:
            result = request.env["partner.registration.field"].replace_partner_fields(
                partner,
                fields_payload,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "invalid_request", "message": error.args[0]},
                status=400,
            )

        return json_response(result)

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )
        if not isinstance(payload, dict):
            return None, json_response(
                {"error": "invalid_json", "message": "Body must be a JSON object."},
                status=400,
            )
        return payload, None
