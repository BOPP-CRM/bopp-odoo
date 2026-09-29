from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalLazadaController(http.Controller):
    @http.route("/api/portal/lazada", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_lazada_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        return json_response({"lazada": partner.serialize_lazada_status()})

    @http.route("/api/portal/lazada/connect/start", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def start_lazada_connect(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        try:
            result = partner.start_lazada_oauth_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response(result)

    @http.route("/api/portal/lazada/disable", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def disable_lazada(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        return json_response({"lazada": partner.disable_lazada_for_api()})
