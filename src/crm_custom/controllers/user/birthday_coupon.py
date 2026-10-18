from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.datetime import to_thailand_string
from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response


class BirthdayCouponController(http.Controller):
    @http.route(
        "/api/partner/<string:slug>/user/birthday-coupon",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_birthday_coupon(self, slug, **kwargs):
        context, error = self._resolve(slug)
        if error:
            return error
        rule = context["rule"]
        if not rule:
            return json_response({"birthday_coupon": False})
        return json_response({"birthday_coupon": rule.serialize_for_user(context["user"])})

    @http.route(
        "/api/partner/<string:slug>/user/birthday-coupon/claim",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def claim_birthday_coupon(self, slug, **kwargs):
        context, error = self._resolve(slug)
        if error:
            return error
        rule = context["rule"]
        if not rule:
            return json_response(
                {"error": "birthday_reward_unavailable", "message": "ยังไม่เปิดใช้งานสิทธิพิเศษวันเกิด"},
                status=400,
            )

        try:
            user_coupon = rule.claim_for_user(context["user"])
        except ValidationError as claim_error:
            request.env.cr.rollback()
            return json_response(
                {"error": "birthday_claim_not_allowed", "message": str(claim_error)},
                status=400,
            )

        return json_response({"coupon": self._serialize_user_coupon(user_coupon)})

    # ------------------------------------------------------------------
    def _resolve(self, slug):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return None, auth_error

        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return None, json_response(
                {"error": "partner_not_found", "message": "ไม่พบ Client ดังกล่าว"},
                status=404,
            )

        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", line_profile["userId"]),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return None, json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        rule = request.env["partner.birthday.reward"].sudo().search([
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], limit=1)
        return {"partner": partner, "user": user, "rule": rule}, None

    def _serialize_user_coupon(self, coupon):
        return {
            "id": coupon.id,
            "name": coupon.name,
            "code": coupon.code,
            "value": coupon.value,
            "acquired_date": to_thailand_string(coupon.acquired_date),
            "activated_date": to_thailand_string(coupon.activated_date),
            "expiration_date": to_thailand_string(coupon.expiration_date),
            "state": coupon.state,
            "is_used": coupon.is_used,
            "used_date": to_thailand_string(coupon.used_date),
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
