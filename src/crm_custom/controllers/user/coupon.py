import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.datetime import to_thailand_string
from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response


class CouponController(http.Controller):
    @http.route("/api/partner/<string:slug>/coupon", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_coupon(self, slug, **kwargs):
        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        now = fields.Datetime.now()
        coupons = request.env["partner.coupon"].sudo().search([
            ("partner_id", "=", partner_response["partner"].id),
            ("is_show_in_ui", "=", True),
            ("start_time", "<=", now),
            "|",
            ("end_time", "=", False),
            ("end_time", ">=", now),
        ])

        return json_response({
            "coupon": [self._serialize_partner_coupon(coupon) for coupon in coupons],
        })

    @http.route("/api/partner/<string:slug>/coupon/<int:coupon_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_coupon_by_id(self, slug, coupon_id, **kwargs):
        coupon_response = self._get_coupon(slug, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        return json_response({
            "coupon": self._serialize_partner_coupon(coupon_response["coupon"]),
        })

    @http.route("/api/partner/<string:slug>/coupon/<int:coupon_id>/redeem", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def redeem_coupon(self, slug, coupon_id, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        coupon_response = self._get_coupon(slug, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        user_response = self._get_user(coupon_response["partner"], line_profile["userId"])
        if user_response["error"]:
            return user_response["error"]

        try:
            user_coupon = coupon_response["coupon"].sudo().redeem_for_user(user_response["user"])
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_user_coupon(user_coupon),
        })

    @http.route("/api/partner/<string:slug>/user/coupon", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_user_coupon(self, slug, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        user_response = self._get_user(partner_response["partner"], line_profile["userId"])
        if user_response["error"]:
            return user_response["error"]

        coupons = user_response["user"].coupon_ids.sorted(
            key=lambda coupon: coupon.acquired_date,
            reverse=True,
        )
        return json_response({
            "coupon": [self._serialize_user_coupon(coupon) for coupon in coupons],
        })

    @http.route("/api/partner/<string:slug>/user/coupon/<string:coupon_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_user_coupon_by_id(self, slug, coupon_id, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        user_response = self._get_user(partner_response["partner"], line_profile["userId"])
        if user_response["error"]:
            return user_response["error"]

        coupon = user_response["user"].coupon_ids.filtered(
            lambda coupon: str(coupon.id) == coupon_id
        )

        if not coupon:
            return json_response(
                {"error": "coupon_not_found", "message": "ไม่พบคูปองดังกล่าว"},
                status=404,
            )

        return json_response({
            "coupon": self._serialize_user_coupon(coupon),
        })

    @http.route("/api/partner/<string:slug>/user/coupon/<string:code>/activate", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def activate_user_coupon(self, slug, code, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        user_response = self._get_user(partner_response["partner"], line_profile["userId"])
        if user_response["error"]:
            return user_response["error"]

        user_coupon = request.env["crm.user.coupon"].sudo().search([
            ("partner_id", "=", partner_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
            ("code", "=", code),
        ], limit=1)
        if not user_coupon:
            return json_response(
                {"error": "coupon_not_found", "message": "ไม่พบ coupon ดังกล่าว"},
                status=404,
            )

        try:
            user_coupon.action_activate()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_user_coupon(user_coupon),
        })

    @http.route("/api/partner/<string:slug>/user/coupon/<string:code>/use", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def use_user_coupon(self, slug, code, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        user_response = self._get_user(partner_response["partner"], line_profile["userId"])
        if user_response["error"]:
            return user_response["error"]

        user_coupon = request.env["crm.user.coupon"].sudo().search([
            ("partner_id", "=", partner_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
            ("code", "=", code),
        ], limit=1)
        if not user_coupon:
            return json_response(
                {"error": "coupon_not_found", "message": "ไม่พบ coupon ดังกล่าว"},
                status=404,
            )

        try:
            user_coupon.action_mark_used()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_user_coupon(user_coupon),
        })

    def _get_partner(self, slug):
        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return {
                "partner": False,
                "error": json_response(
                    {"error": "partner_not_found", "message": "ไม่พบ Client กรุณาลองอีกครั้งหรือติดต่อเจ้าหน้าที่"},
                    status=404,
                ),
            }

        return {
            "partner": partner,
            "error": False,
        }

    def _get_coupon(self, slug, coupon_id):
        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return {
                "partner": False,
                "coupon": False,
                "error": partner_response["error"],
            }

        coupon = request.env["partner.coupon"].sudo().search([
            ("id", "=", coupon_id),
            ("partner_id", "=", partner_response["partner"].id),
        ], limit=1)
        if not coupon:
            return {
                "partner": partner_response["partner"],
                "coupon": False,
                "error": json_response(
                    {"error": "coupon_not_found", "message": "ไม่พบ Coupon ดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "partner": partner_response["partner"],
            "coupon": coupon,
            "error": False,
        }

    def _get_user(self, partner, user_id):
        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", user_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return {
                "user": False,
                "error": json_response(
                    {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "user": user,
            "error": False,
        }

    def _serialize_partner_coupon(self, coupon):
        return {
            "id": coupon.id,
            "name": coupon.name,
            "image_url": coupon.image or False,
            "value": coupon.value,
            "term_and_condition": coupon.term_and_condition,
            "start_time": to_thailand_string(coupon.start_time),
            "end_time": to_thailand_string(coupon.end_time),
            "code_expiry_interval": coupon.code_expiry_interval,
            "redeemed_count": coupon.redeemed_count,
            "is_show_in_ui": coupon.is_show_in_ui,
            "max_redeem_per_user": coupon.max_redeem_per_user,
            "currency": {
                "id": coupon.currency_id.id,
                "name": coupon.currency_id.name,
                "is_default": coupon.currency_id.is_default,
            },
        }

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
            "point": {
                "id": coupon.point_id.id,
                "value": coupon.point_id.value,
                "type": coupon.point_id.type,
            } if coupon.point_id else False,
        }
