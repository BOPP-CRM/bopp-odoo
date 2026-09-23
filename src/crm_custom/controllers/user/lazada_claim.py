import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response


class LazadaClaimController(http.Controller):
    @http.route(
        "/api/partner/<string:slug>/user/lazada-claim",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def submit_lazada_claim(self, slug, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return json_response(
                {"error": "partner_not_found", "message": "ไม่พบ Client ดังกล่าว"},
                status=404,
            )

        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", line_profile["userId"]),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response({"error": "invalid_json", "message": "Invalid JSON body."}, status=400)
        if not isinstance(payload, dict):
            payload = {}

        order_number = payload.get("order_number") or payload.get("orderNumber")

        try:
            claim = request.env["partner.lazada.order.claim"].sudo().verify_order_for_points(
                partner, user, order_number,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "claim_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({"claim": claim.serialize_for_portal()}, status=201)
