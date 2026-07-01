import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalUsersController(http.Controller):
    @http.route("/api/portal/users", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_users(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        domain = [("partner_id", "=", partner.id)]

        search_term = (kwargs.get("search") or "").strip()
        if search_term:
            domain += [
                "|", "|", "|",
                ("display_name", "ilike", search_term),
                ("line_user_id", "ilike", search_term),
                ("email", "ilike", search_term),
                ("phone", "ilike", search_term),
            ]

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        users = request.env["crm.user"].sudo().search(
            domain,
            limit=limit,
            offset=offset,
            order="create_date desc",
        )
        total = request.env["crm.user"].sudo().search_count(domain)

        return json_response({
            "users": [self._serialize_user(user) for user in users],
            "total": total,
        })

    @http.route("/api/portal/users/<int:user_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_user(self, user_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user(user.crm_partner_id, user_id)
        if user_response["error"]:
            return user_response["error"]

        return json_response({
            "user": self._serialize_user(user_response["user"]),
        })

    @http.route("/api/portal/users/<int:user_id>/point", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def adjust_user_point(self, user_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id

        user_response = self._get_user(partner, user_id)
        if user_response["error"]:
            return user_response["error"]

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        currency_response = self._get_currency(partner, payload.get("currency_id"))
        if currency_response["error"]:
            return currency_response["error"]

        try:
            point = user_response["user"].adjust_point(
                payload.get("value") or 0,
                payload.get("type"),
                currency_response["currency"],
                payload.get("note"),
                payload.get("expiration_date") or None,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "point_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "point": self._serialize_point(point),
        }, status=201)

    @http.route("/api/portal/users/<int:user_id>/points", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_user_points(self, user_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        user_response = self._get_user(partner, user_id)
        if user_response["error"]:
            return user_response["error"]

        domain = [("user_id", "=", user_response["user"].id)]

        currency_id = self._parse_int(kwargs.get("currency_id"))
        if currency_id:
            currency_response = self._get_currency(partner, currency_id)
            if currency_response["error"]:
                return currency_response["error"]
            domain.append(("currency_id", "=", currency_response["currency"].id))

        point_type = (kwargs.get("type") or "").strip()
        if point_type in {"earn", "transfer", "burn"}:
            domain.append(("type", "=", point_type))

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        points = request.env["crm.user.point"].sudo().search(
            domain,
            limit=limit,
            offset=offset,
            order="given_date desc, id desc",
        )
        total = request.env["crm.user.point"].sudo().search_count(domain)

        return json_response({
            "points": [self._serialize_point_history(point) for point in points],
            "total": total,
        })

    @http.route("/api/portal/users/<int:user_id>/coupons", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_user_coupons(self, user_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user(user.crm_partner_id, user_id)
        if user_response["error"]:
            return user_response["error"]

        domain = [("user_id", "=", user_response["user"].id)]

        is_used = kwargs.get("is_used")
        if is_used is not None:
            if str(is_used).lower() in {"1", "true", "yes"}:
                domain.append(("is_used", "=", True))
            elif str(is_used).lower() in {"0", "false", "no"}:
                domain.append(("is_used", "=", False))

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        coupons = request.env["crm.user.coupon"].sudo().search(
            domain,
            limit=limit,
            offset=offset,
            order="acquired_date desc, id desc",
        )
        total = request.env["crm.user.coupon"].sudo().search_count(domain)

        return json_response({
            "coupons": [self._serialize_user_coupon(coupon) for coupon in coupons],
            "total": total,
        })

    @http.route("/api/portal/users/<int:user_id>/coupons/<int:coupon_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_user_coupon(self, user_id, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user(user.crm_partner_id, user_id)
        if user_response["error"]:
            return user_response["error"]

        coupon = request.env["crm.user.coupon"].sudo().search([
            ("id", "=", coupon_id),
            ("user_id", "=", user_response["user"].id),
        ], limit=1)
        if not coupon:
            return json_response(
                {"error": "coupon_not_found", "message": "ไม่พบคูปองดังกล่าว"},
                status=404,
            )

        return json_response({
            "coupon": self._serialize_user_coupon(coupon),
        })

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get_user(self, partner, user_id):
        user = request.env["crm.user"].sudo().search([
            ("id", "=", user_id),
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

    def _get_currency(self, partner, currency_id):
        if currency_id:
            currency = request.env["crm.partner.currency"].sudo().search([
                ("id", "=", currency_id),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not currency:
                return {
                    "currency": False,
                    "error": json_response(
                        {"error": "currency_not_found", "message": "ไม่พบ Currency ดังกล่าว"},
                        status=404,
                    ),
                }
            return {"currency": currency, "error": False}

        currency = (
            partner.currency_ids.filtered("is_default")[:1]
            or partner.currency_ids[:1]
        )
        if not currency:
            return {
                "currency": False,
                "error": json_response(
                    {"error": "currency_not_found", "message": "Partner ยังไม่มี Currency"},
                    status=404,
                ),
            }
        return {"currency": currency, "error": False}

    def _serialize_user(self, user):
        return {
            "id": user.id,
            "display_name": user.display_name,
            "picture_url": user.picture_url,
            "line_user_id": user.line_user_id,
            "email": user.email,
            "is_email_verified": user.is_email_verified,
            "phone": user.phone,
            "is_phone_verified": user.is_phone_verified,
            "birth_date": fields.Date.to_string(user.birth_date) if user.birth_date else False,
            "gender": user.gender,
            "address": user.address or False,
            "is_updated_user_info": user.is_updated_user_info,
            "tier": self._serialize_tier(user.tier_id),
            "points": self._serialize_balances(user),
            "create_date": fields.Datetime.to_string(user.create_date),
        }

    def _serialize_tier(self, tier):
        if not tier:
            return False

        return {
            "code": tier.code,
            "name": tier.name,
        }

    def _serialize_balances(self, user):
        balances = []
        for currency in user.partner_id.currency_ids:
            balances.append({
                "currency": {
                    "id": currency.id,
                    "name": currency.name,
                    "is_default": currency.is_default,
                    "is_total_spending": currency.is_total_spending,
                },
                "balance": user._get_currency_balance(currency),
            })
        return balances

    def _serialize_point(self, point):
        return self._serialize_point_history(point)

    def _serialize_point_history(self, point):
        data = {
            "id": point.id,
            "name": point.name,
            "value": point.value,
            "type": point.type,
            "admin_note": point.admin_note or False,
            "given_date": fields.Datetime.to_string(point.given_date),
            "expiration_date": fields.Datetime.to_string(point.expiration_date) if point.expiration_date else False,
            "currency": {
                "id": point.currency_id.id,
                "name": point.currency_id.name,
                "is_default": point.currency_id.is_default,
                "is_total_spending": point.currency_id.is_total_spending,
            },
            "source": self._serialize_point_source(point),
        }
        return data

    def _serialize_point_source(self, point):
        if point.point_redeem_id:
            return {
                "kind": "redeem_qr",
                "id": point.point_redeem_id.id,
                "name": point.point_redeem_id.name,
                "code": point.point_redeem_id.code,
            }
        if point.receipt_redeem_id:
            return {
                "kind": "receipt",
                "id": point.receipt_redeem_id.id,
                "receipt_number": point.receipt_redeem_id.receipt_number,
            }
        if point.member_reward_id:
            return {
                "kind": "member_reward",
                "id": point.member_reward_id.id,
                "event": point.member_reward_event,
                "name": point.member_reward_id.name,
            }
        if point.admin_note:
            return {
                "kind": "admin",
                "note": point.admin_note,
            }
        return False

    def _serialize_user_coupon(self, coupon):
        return {
            "id": coupon.id,
            "name": coupon.name,
            "code": coupon.code,
            "value": coupon.value,
            "admin_note": coupon.admin_note or False,
            "acquired_date": fields.Datetime.to_string(coupon.acquired_date),
            "activated_date": fields.Datetime.to_string(coupon.activated_date) if coupon.activated_date else False,
            "expiration_date": fields.Datetime.to_string(coupon.expiration_date) if coupon.expiration_date else False,
            "state": coupon.state,
            "is_used": coupon.is_used,
            "used_date": fields.Datetime.to_string(coupon.used_date) if coupon.used_date else False,
            "coupon": {
                "id": coupon.coupon_id.id,
                "name": coupon.coupon_id.name,
                "term_and_condition": coupon.coupon_id.term_and_condition or False,
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
            "source": self._serialize_coupon_source(coupon),
        }

    def _serialize_coupon_source(self, coupon):
        if coupon.point_redeem_id:
            return {
                "kind": "redeem_qr",
                "id": coupon.point_redeem_id.id,
                "name": coupon.point_redeem_id.name,
                "code": coupon.point_redeem_id.code,
            }
        if coupon.member_reward_id:
            return {
                "kind": "member_reward",
                "id": coupon.member_reward_id.id,
                "event": coupon.member_reward_event,
                "name": coupon.member_reward_id.name,
            }
        if coupon.admin_note:
            return {
                "kind": "admin",
                "note": coupon.admin_note,
            }
        if coupon.point_id:
            return {
                "kind": "redeem",
                "point_id": coupon.point_id.id,
            }
        return False
