import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.request import json_response


class RedeemPointController(http.Controller):
    @http.route("/api/partner/<string:slug>/redeem/<string:code>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_redeem(self, slug, code, **kwargs):
        redeem_response = self._get_redeem(slug, code)
        if redeem_response["error"]:
            return redeem_response["error"]

        return json_response({
            "redeem": self._serialize_redeem(redeem_response["redeem"]),
        })

    @http.route("/api/partner/<string:slug>/redeem/<string:code>", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def redeem(self, slug, code, **kwargs):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        redeem_response = self._get_redeem(slug, code)
        if redeem_response["error"]:
            return redeem_response["error"]

        user_id = payload.get("userId") or payload.get("line_user_id")
        user = request.env["crm.user"].sudo().search(
            [
                ("line_user_id", "=", user_id),
                ("partner_id", "=", redeem_response["partner"].id),
            ],
            limit=1,
        )
        if not user:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        try:
            result = redeem_response["redeem"].sudo().redeem_for_user(user)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "redeem_not_allowed", "message": str(error)},
                status=400,
            )

        response = {}
        point = result.get("point")
        user_coupon = result.get("user_coupon")
        if point:
            response["point"] = self._serialize_point(point)
        if user_coupon:
            response["coupon"] = self._serialize_user_coupon(user_coupon)

        return json_response(response)

    def _get_redeem(self, slug, code):
        partner = request.env["partner"].sudo().search(
            [("slug", "=", slug)],
            limit=1,
        )
        if not partner:
            return {
                "partner": False,
                "redeem": False,
                "error": json_response(
                    {"error": "partner_not_found", "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่"},
                    status=404,
                ),
            }

        redeem = request.env["crm.partner.point.redeem"].sudo().search(
            [
                ("partner_id", "=", partner.id),
                ("code", "=", code),
            ],
            limit=1,
        )
        if not redeem:
            return {
                "partner": partner,
                "redeem": False,
                "error": json_response(
                    {"error": "redeem_not_found", "message": "QR Code ไม่ถูกต้อง"},
                    status=404,
                ),
            }

        return {
            "partner": partner,
            "redeem": redeem,
            "error": False,
        }

    def _serialize_redeem(self, redeem):
        data = {
            "code": redeem.code,
            "name": redeem.name,
            "value": redeem.value,
            "type": redeem.type,
            "limit_per_user": redeem.limit_per_user,
            "limit_per_qr": redeem.limit_per_qr,
            "redeemed_count": redeem.redeemed_count,
            "expiration_date": fields.Datetime.to_string(redeem.expiration_date),
            "active": redeem.active,
            "redeem_url": redeem.redeem_url or False,
            "qr_code_url": redeem.get_qr_code_url(),
            "currency": {
                "id": redeem.currency_id.id,
                "name": redeem.currency_id.name,
                "is_default": redeem.currency_id.is_default,
            },
        }
        if redeem.reward_coupon_id:
            data["reward_coupon"] = {
                "id": redeem.reward_coupon_id.id,
                "name": redeem.reward_coupon_id.name,
                "image_url": redeem.reward_coupon_id.image or False,
                "value": redeem.reward_coupon_id.value,
            }
        else:
            data["reward_coupon"] = False
        return data

    def _serialize_point(self, point):
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
            },
        }

    def _serialize_user_coupon(self, coupon):
        return {
            "id": coupon.id,
            "name": coupon.name,
            "code": coupon.code,
            "value": coupon.value,
            "acquired_date": fields.Datetime.to_string(coupon.acquired_date),
            "activated_date": fields.Datetime.to_string(coupon.activated_date) if coupon.activated_date else False,
            "expiration_date": fields.Datetime.to_string(coupon.expiration_date) if coupon.expiration_date else False,
            "state": coupon.state,
            "is_used": coupon.is_used,
            "used_date": fields.Datetime.to_string(coupon.used_date) if coupon.used_date else False,
            "coupon": {
                "id": coupon.coupon_id.id,
                "name": coupon.coupon_id.name,
                "term_and_condition": coupon.coupon_id.term_and_condition,
                "image_url": coupon.coupon_id.image or False,
            },
            "currency": {
                "id": coupon.currency_id.id,
                "name": coupon.currency_id.name,
                "is_default": coupon.currency_id.is_default,
            },
        }
