import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.request import json_response


class OmisellClaimController(http.Controller):
    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/omisell-claim",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def submit_claim(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        platform = payload.get("platform")
        order_number = payload.get("orderNumber") or payload.get("order_number")

        try:
            claim = request.env["partner.omisell.order.claim"].sudo().submit_claim(
                user_response["partner"],
                user_response["user"],
                platform,
                order_number,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "claim_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "claim": self._serialize_claim(claim),
        }, status=201)

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/omisell-claim",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_claims(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        claims = request.env["partner.omisell.order.claim"].sudo().search([
            ("partner_id", "=", user_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
        ], order="submitted_date desc, id desc")

        return json_response({
            "claims": [self._serialize_claim(claim) for claim in claims],
        })

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/omisell-claim/<int:claim_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_claim(self, slug, user_id, claim_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        claim_response = self._get_claim(user_response["partner"], user_response["user"], claim_id)
        if claim_response["error"]:
            return claim_response["error"]

        return json_response({
            "claim": self._serialize_claim(claim_response["claim"]),
        })

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/omisell-claim/<int:claim_id>/recheck",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def recheck_claim(self, slug, user_id, claim_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        claim_response = self._get_claim(user_response["partner"], user_response["user"], claim_id)
        if claim_response["error"]:
            return claim_response["error"]

        claim = claim_response["claim"]
        try:
            claim.sudo().recheck()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "claim_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "claim": self._serialize_claim(claim),
        })

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )
        if not isinstance(payload, dict):
            payload = {}
        return payload, None

    def _get_user(self, slug, user_id):
        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return {
                "partner": False,
                "user": False,
                "error": json_response(
                    {
                        "error": "partner_not_found",
                        "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่",
                    },
                    status=404,
                ),
            }

        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", user_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return {
                "partner": partner,
                "user": False,
                "error": json_response(
                    {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "partner": partner,
            "user": user,
            "error": False,
        }

    def _get_claim(self, partner, user, claim_id):
        claim = request.env["partner.omisell.order.claim"].sudo().search([
            ("id", "=", claim_id),
            ("partner_id", "=", partner.id),
            ("user_id", "=", user.id),
        ], limit=1)
        if not claim:
            return {
                "claim": False,
                "error": json_response(
                    {"error": "claim_not_found", "message": "ไม่พบรายการดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "claim": claim,
            "error": False,
        }

    def _serialize_claim(self, claim):
        order = claim.omisell_order_id
        return {
            "id": claim.id,
            "platform": claim.platform,
            "order_number": claim.order_number,
            "omisell_order_number": claim.omisell_order_number or False,
            "order_status_id": claim.order_status_id or False,
            "order_status_name": claim.order_status_name or False,
            "return_order_number": order.omisell_return_order_number or False if order else False,
            "return_order_status_name": order.return_order_status_name or False if order else False,
            "amount": order.amount if order else 0,
            "state": claim.state,
            "reject_reason": claim.reject_reason or False,
            "error_message": claim.error_message or False,
            "submitted_date": fields.Datetime.to_string(claim.submitted_date),
            "reviewed_date": fields.Datetime.to_string(claim.reviewed_date) if claim.reviewed_date else False,
            "last_checked_at": fields.Datetime.to_string(claim.last_checked_at) if claim.last_checked_at else False,
            "spending_point": self._serialize_point(claim.spending_point_id),
            "reward_point": self._serialize_point(claim.reward_point_id),
        }

    def _serialize_point(self, point):
        if not point:
            return False

        return {
            "id": point.id,
            "name": point.name,
            "value": point.value,
            "type": point.type,
            "given_date": fields.Datetime.to_string(point.given_date),
            "currency": {
                "id": point.currency_id.id,
                "name": point.currency_id.name,
                "is_default": point.currency_id.is_default,
                "is_total_spending": point.currency_id.is_total_spending,
            },
        }
