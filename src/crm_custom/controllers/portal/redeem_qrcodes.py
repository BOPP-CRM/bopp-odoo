import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request
from psycopg2 import IntegrityError

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

ALLOWED_REDEEM_CREATE_FIELDS = (
    "name",
    "value",
    "type",
    "currency_id",
    "limit_per_user",
    "limit_per_qr",
    "expiration_date",
    "reward_coupon_id",
    "active",
)

ALLOWED_REDEEM_UPDATE_FIELDS = (
    "name",
    "value",
    "type",
    "currency_id",
    "limit_per_user",
    "limit_per_qr",
    "expiration_date",
    "reward_coupon_id",
    "active",
)


class PortalRedeemQRCodesController(http.Controller):
    @http.route("/api/portal/redeem-qrcodes", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_redeem_qrcodes(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        domain = [("partner_id", "=", user.crm_partner_id.id)]

        active = kwargs.get("active")
        if active is not None:
            if str(active).lower() in {"1", "true", "yes"}:
                domain.append(("active", "=", True))
            elif str(active).lower() in {"0", "false", "no"}:
                domain.append(("active", "=", False))

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        redeem_model = request.env["crm.partner.point.redeem"].sudo()
        redeems = redeem_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="create_date desc, id desc",
        )
        total = redeem_model.search_count(domain)

        return json_response({
            "redeem_qrcodes": [
                self._serialize_redeem(redeem, include_qr=True)
                for redeem in redeems
            ],
            "total": total,
        })

    @http.route("/api/portal/redeem-qrcodes/<int:redeem_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_redeem_qrcode(self, redeem_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        redeem_response = self._get_redeem(user.crm_partner_id, redeem_id)
        if redeem_response["error"]:
            return redeem_response["error"]

        return json_response({
            "redeem_qrcode": self._serialize_redeem(
                redeem_response["redeem"],
                include_qr=True,
            ),
        })

    @http.route("/api/portal/redeem-qrcodes", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_redeem_qrcode(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        if not payload.get("name"):
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ name"},
                status=400,
            )

        currency_response = self._get_currency(partner, payload.get("currency_id"))
        if currency_response["error"]:
            return currency_response["error"]

        vals = self._extract_redeem_vals(payload, ALLOWED_REDEEM_CREATE_FIELDS)
        vals["partner_id"] = partner.id
        vals["currency_id"] = currency_response["currency"].id

        try:
            self._validate_reward_coupon(partner, vals.get("reward_coupon_id"))
            redeem = request.env["crm.partner.point.redeem"].sudo().create(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "redeem_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "redeem_not_allowed", "message": "ข้อมูล Redeem QR ไม่ถูกต้อง"},
                status=400,
            )

        return json_response({
            "redeem_qrcode": self._serialize_redeem(redeem, include_qr=True),
        }, status=201)

    @http.route("/api/portal/redeem-qrcodes/<int:redeem_id>", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_redeem_qrcode(self, redeem_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        redeem_response = self._get_redeem(partner, redeem_id)
        if redeem_response["error"]:
            return redeem_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = self._extract_redeem_vals(payload, ALLOWED_REDEEM_UPDATE_FIELDS)
        if payload.get("currency_id") is not None:
            currency_response = self._get_currency(partner, payload.get("currency_id"))
            if currency_response["error"]:
                return currency_response["error"]
            vals["currency_id"] = currency_response["currency"].id

        if not vals:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            if "reward_coupon_id" in vals:
                self._validate_reward_coupon(partner, vals.get("reward_coupon_id"))
            redeem_response["redeem"].sudo().write(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "redeem_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "redeem_not_allowed", "message": "ข้อมูล Redeem QR ไม่ถูกต้อง"},
                status=400,
            )

        return json_response({
            "redeem_qrcode": self._serialize_redeem(
                redeem_response["redeem"],
                include_qr=True,
            ),
        })

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        return payload, None

    def _extract_redeem_vals(self, payload, allowed_fields):
        return {
            field: payload[field]
            for field in allowed_fields
            if field in payload
        }

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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

    def _validate_reward_coupon(self, partner, coupon_id):
        if not coupon_id:
            return

        coupon = request.env["partner.coupon"].sudo().search([
            ("id", "=", coupon_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not coupon:
            raise ValidationError("ไม่พบ Coupon ดังกล่าว")

    def _get_redeem(self, partner, redeem_id):
        redeem = request.env["crm.partner.point.redeem"].sudo().search([
            ("id", "=", redeem_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not redeem:
            return {
                "redeem": False,
                "error": json_response(
                    {"error": "redeem_not_found", "message": "ไม่พบ Redeem QR ดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "redeem": redeem,
            "error": False,
        }

    def _serialize_redeem(self, redeem, include_qr=False):
        data = {
            "id": redeem.id,
            "name": redeem.name,
            "code": redeem.code,
            "value": redeem.value,
            "type": redeem.type,
            "limit_per_user": redeem.limit_per_user,
            "limit_per_qr": redeem.limit_per_qr,
            "redeemed_count": redeem.redeemed_count,
            "expiration_date": fields.Datetime.to_string(redeem.expiration_date) if redeem.expiration_date else False,
            "active": redeem.active,
            "currency_id": redeem.currency_id.id,
            "currency_name": redeem.currency_id.name,
            "redeem_url": redeem.redeem_url or False,
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

        if include_qr:
            data["qr_code_url"] = redeem.get_qr_code_url()

        return data
