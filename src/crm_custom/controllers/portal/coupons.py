import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request
from psycopg2 import IntegrityError

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import csv_response, json_response

MAX_CODE_BATCH_SIZE = 2000

ALLOWED_COUPON_CREATE_FIELDS = (
    "name",
    "currency_id",
    "value",
    "image",
    "term_and_condition",
    "start_time",
    "end_time",
    "code_expiry_interval",
    "code_source",
    "prefix_code",
    "random_range",
    "suffix_code",
    "is_show_in_ui",
    "max_redeem_per_user",
)

ALLOWED_COUPON_UPDATE_FIELDS = (
    "name",
    "value",
    "image",
    "term_and_condition",
    "start_time",
    "end_time",
    "code_expiry_interval",
    "prefix_code",
    "random_range",
    "suffix_code",
    "is_show_in_ui",
    "max_redeem_per_user",
)


class PortalCouponsController(http.Controller):
    @http.route("/api/portal/coupons", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_coupons(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        coupons = request.env["partner.coupon"].sudo().search([
            ("partner_id", "=", user.crm_partner_id.id),
        ], order="create_date desc")

        return json_response({
            "coupons": [self._serialize_coupon(coupon) for coupon in coupons],
        })

    @http.route("/api/portal/coupons/<int:coupon_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_coupon(self, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        coupon_response = self._get_coupon(user.crm_partner_id, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        return json_response({
            "coupon": self._serialize_coupon(
                coupon_response["coupon"],
                include_redemption_summary=True,
            ),
        })

    @http.route("/api/portal/coupons/<int:coupon_id>/redemptions", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_coupon_redemptions(self, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        coupon_response = self._get_coupon(user.crm_partner_id, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        coupon = coupon_response["coupon"]
        domain = [("coupon_id", "=", coupon.id)]

        is_used = kwargs.get("is_used")
        if is_used is not None:
            if str(is_used).lower() in {"1", "true", "yes"}:
                domain.append(("is_used", "=", True))
            elif str(is_used).lower() in {"0", "false", "no"}:
                domain.append(("is_used", "=", False))

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        user_coupons = request.env["crm.user.coupon"].sudo().search(
            domain,
            limit=limit,
            offset=offset,
            order="acquired_date desc, id desc",
        )
        total = request.env["crm.user.coupon"].sudo().search_count(domain)

        return json_response({
            "coupon": {
                "id": coupon.id,
                "name": coupon.name,
            },
            "summary": self._serialize_redemption_summary(coupon),
            "redemptions": [
                self._serialize_redemption(user_coupon)
                for user_coupon in user_coupons
            ],
            "total": total,
        })

    @http.route("/api/portal/coupons", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_coupon(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        currency_response = self._get_currency(partner, payload.get("currency_id"))
        if currency_response["error"]:
            return currency_response["error"]

        vals = self._extract_coupon_vals(payload, ALLOWED_COUPON_CREATE_FIELDS)
        vals["partner_id"] = partner.id
        vals["currency_id"] = currency_response["currency"].id

        code_source = vals.get("code_source", "generate")
        code_quantity = payload.get("code_quantity", 1)
        random_range = vals.get("random_range", 6)

        try:
            image_url = self._resolve_image_url(partner, payload)
            if image_url is not None:
                vals["image"] = image_url

            if code_source == "generate":
                request.env["partner.coupon"]._validate_code_batch_size(code_quantity)
                if random_range <= 0:
                    raise ValidationError("Random range ต้องมากกว่า 0")
            elif not payload.get("import_file"):
                raise ValidationError("กรุณาส่ง import_file สำหรับ code_source = import")

            if code_source == "generate":
                vals["locked_random_range"] = random_range

            coupon = request.env["partner.coupon"].sudo().create(vals)

            if code_source == "generate":
                coupon.create_generated_codes(code_quantity, random_range)
            else:
                coupon.import_codes_from_file(
                    payload.get("import_file"),
                    payload.get("import_filename"),
                )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": "ข้อมูล Coupon ไม่ถูกต้อง"},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_coupon(coupon),
        }, status=201)

    @http.route("/api/portal/coupons/<int:coupon_id>", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_coupon(self, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        coupon_response = self._get_coupon(partner, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = self._extract_coupon_vals(payload, ALLOWED_COUPON_UPDATE_FIELDS)
        if payload.get("currency_id") is not None:
            currency_response = self._get_currency(partner, payload.get("currency_id"))
            if currency_response["error"]:
                return currency_response["error"]
            vals["currency_id"] = currency_response["currency"].id

        try:
            image_url = self._resolve_image_url(partner, payload)
            if image_url is not None:
                vals["image"] = image_url

            if not vals:
                return json_response(
                    {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                    status=400,
                )

            coupon_response["coupon"].sudo().write(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": "ข้อมูล Coupon ไม่ถูกต้อง"},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_coupon(coupon_response["coupon"]),
        })

    @http.route(
        "/api/portal/coupons/codes/import-template",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def download_coupon_codes_import_template(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        csv_content = request.env["partner.coupon"].get_import_template_csv_content()
        return csv_response(csv_content, "coupon_code_import_template.csv")

    @http.route(
        "/api/portal/coupons/<int:coupon_id>/codes",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def add_coupon_codes(self, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        coupon_response = self._get_coupon(user.crm_partner_id, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        add_source = payload.get("add_source", "generate")
        if add_source not in {"generate", "import"}:
            return json_response(
                {"error": "invalid_request", "message": "add_source ต้องเป็น generate หรือ import"},
                status=400,
            )

        coupon = coupon_response["coupon"]
        try:
            added_count = coupon.sudo().add_codes(
                add_source=add_source,
                code_quantity=self._parse_int(payload.get("code_quantity")) or 1,
                prefix_code=payload.get("prefix_code"),
                random_range=self._parse_int(payload.get("random_range")),
                suffix_code=payload.get("suffix_code"),
                import_file=payload.get("import_file"),
                import_filename=payload.get("import_filename"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "coupon_not_allowed", "message": "ข้อมูล Coupon Code ไม่ถูกต้อง"},
                status=400,
            )

        return json_response({
            "coupon": self._serialize_coupon(coupon),
            "added_code_count": added_count,
        })

    @http.route(
        "/api/portal/coupons/<int:coupon_id>/codes/export",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def export_coupon_codes(self, coupon_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        coupon_response = self._get_coupon(user.crm_partner_id, coupon_id)
        if coupon_response["error"]:
            return coupon_response["error"]

        coupon = coupon_response["coupon"]
        return csv_response(
            coupon.get_codes_export_csv_content(),
            coupon.get_codes_export_filename(),
        )

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        return payload, None

    def _extract_coupon_vals(self, payload, allowed_fields):
        return {
            field: payload[field]
            for field in allowed_fields
            if field in payload
        }

    def _resolve_image_url(self, partner, payload):
        image_base64 = payload.get("image_base64")
        if image_base64:
            if not str(image_base64).strip():
                raise ValidationError("กรุณาส่ง image_base64")

            coupon = request.env["partner.coupon"].sudo().new({
                "partner_id": partner.id,
            })
            image_url = coupon._upload_image_field("image", image_base64)
            if not image_url:
                raise ValidationError("อัปโหลดรูปภาพไม่สำเร็จ")
            return image_url

        if "image" in payload:
            image = payload["image"]
            if image and not (
                str(image).startswith("http://") or str(image).startswith("https://")
            ):
                coupon = request.env["partner.coupon"].sudo().new({
                    "partner_id": partner.id,
                })
                image_url = coupon._upload_image_field("image", image)
                if not image_url:
                    raise ValidationError("อัปโหลดรูปภาพไม่สำเร็จ")
                return image_url
            return image or False

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

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get_coupon(self, partner, coupon_id):
        coupon = request.env["partner.coupon"].sudo().search([
            ("id", "=", coupon_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not coupon:
            return {
                "coupon": False,
                "error": json_response(
                    {"error": "coupon_not_found", "message": "ไม่พบ Coupon ดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "coupon": coupon,
            "error": False,
        }

    def _serialize_coupon(self, coupon, include_redemption_summary=False):
        data = {
            "id": coupon.id,
            "name": coupon.name,
            "image_url": coupon.image or False,
            "value": coupon.value,
            "currency_id": coupon.currency_id.id,
            "currency_name": coupon.currency_id.name,
            "code_source": coupon.code_source,
            "prefix_code": coupon.prefix_code or False,
            "random_range": coupon.random_range,
            "suffix_code": coupon.suffix_code or False,
            "start_time": fields.Datetime.to_string(coupon.start_time),
            "end_time": fields.Datetime.to_string(coupon.end_time) if coupon.end_time else False,
            "code_expiry_interval": coupon.code_expiry_interval,
            "term_and_condition": coupon.term_and_condition or False,
            "total_code_count": coupon.total_code_count,
            "available_code_count": coupon.available_code_count,
            "redeemed_count": coupon.redeemed_count,
            "used_code_count": coupon.used_code_count,
            "is_show_in_ui": coupon.is_show_in_ui,
            "max_redeem_per_user": coupon.max_redeem_per_user,
            "max_code_batch_size": MAX_CODE_BATCH_SIZE,
        }
        if include_redemption_summary:
            data["redemption_summary"] = self._serialize_redemption_summary(coupon)
        return data

    def _serialize_redemption_summary(self, coupon):
        user_coupons = coupon.user_coupon_ids
        used_coupons = user_coupons.filtered("is_used")
        return {
            "total_redemptions": len(user_coupons),
            "used_count": len(used_coupons),
            "unused_count": len(user_coupons.filtered(lambda record: not record.is_used)),
            "unique_users": len(user_coupons.mapped("user_id")),
        }

    def _serialize_redemption(self, user_coupon):
        user = user_coupon.user_id
        return {
            "id": user_coupon.id,
            "code": user_coupon.code,
            "value": user_coupon.value,
            "acquired_date": fields.Datetime.to_string(user_coupon.acquired_date),
            "activated_date": fields.Datetime.to_string(user_coupon.activated_date) if user_coupon.activated_date else False,
            "expiration_date": fields.Datetime.to_string(user_coupon.expiration_date) if user_coupon.expiration_date else False,
            "state": user_coupon.state,
            "is_used": user_coupon.is_used,
            "used_date": fields.Datetime.to_string(user_coupon.used_date) if user_coupon.used_date else False,
            "user": {
                "id": user.id,
                "display_name": user.display_name,
                "line_user_id": user.line_user_id,
                "email": user.email or False,
                "phone": user.phone or False,
                "picture_url": user.picture_url or False,
            },
            "source": self._serialize_redemption_source(user_coupon),
        }

    def _serialize_redemption_source(self, user_coupon):
        if user_coupon.point_redeem_id:
            return {
                "kind": "redeem_qr",
                "id": user_coupon.point_redeem_id.id,
                "name": user_coupon.point_redeem_id.name,
                "code": user_coupon.point_redeem_id.code,
            }
        if user_coupon.member_reward_id:
            return {
                "kind": "member_reward",
                "id": user_coupon.member_reward_id.id,
                "event": user_coupon.member_reward_event,
                "name": user_coupon.member_reward_id.name,
            }
        if user_coupon.admin_note:
            return {
                "kind": "admin",
                "note": user_coupon.admin_note,
            }
        if user_coupon.point_id:
            return {
                "kind": "point_redeem",
                "point_id": user_coupon.point_id.id,
            }
        return False
