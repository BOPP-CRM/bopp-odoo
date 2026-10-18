import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

BROADCAST_CONTENT_FIELDS = (
    "name",
    "dry_run",
    "message_type",
    "message_text",
    "flex_payload",
    "attach_image_url",
    "include_coupon",
)
BROADCAST_SEGMENT_FIELDS = (
    "seg_gender",
    "seg_age_min",
    "seg_age_max",
    "seg_signup_from",
    "seg_signup_to",
    "seg_email_verified",
    "seg_phone_verified",
)
BIRTHDAY_REWARD_FIELDS = (
    "name",
    "min_membership_days",
    "message_type",
    "message_text",
    "flex_payload",
    "active",
)


class PortalBroadcastsController(http.Controller):
    # ------------------------------------------------------------------
    # LINE Messaging config
    # ------------------------------------------------------------------
    @http.route("/api/portal/line-messaging", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_line_messaging(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        return json_response({"line_messaging": partner.serialize_line_messaging_status()})

    @http.route("/api/portal/line-messaging/connect", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def connect_line_messaging(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.connect_line_messaging_for_api(payload.get("channel_access_token"))
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response({"line_messaging": status, "message": "เชื่อมต่อ LINE Messaging API สำเร็จ"}, status=201)

    @http.route("/api/portal/line-messaging/disable", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def disable_line_messaging(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        return json_response({"line_messaging": partner.disable_line_messaging_for_api()})

    @http.route("/api/portal/line-messaging/test", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def test_line_messaging(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            result = partner.send_line_test_message_for_api(
                payload.get("line_user_id"),
                payload.get("text"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response(result)

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------
    @http.route("/api/portal/broadcasts/preview", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def preview_broadcast(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()

        try:
            vals = self._broadcast_vals_from_payload(partner, payload, require_name=False)
        except ValidationError as error:
            return json_response({"error": "validation_error", "message": str(error)}, status=400)

        draft = request.env["partner.broadcast"].sudo().new(vals)
        preview = draft.preview_audience()

        coupon_pool = False
        if payload.get("include_coupon") and vals.get("coupon_id"):
            coupon = request.env["partner.coupon"].sudo().browse(vals["coupon_id"])
            coupon_pool = {
                "available": coupon.available_code_count,
                "sufficient": coupon.available_code_count >= preview["count"],
            }

        return json_response({
            "count": preview["count"],
            "sample": preview["sample"],
            "coupon_pool": coupon_pool,
        })

    @http.route("/api/portal/broadcasts", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_broadcast(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()

        try:
            vals = self._broadcast_vals_from_payload(partner, payload, require_name=True)
            broadcast = request.env["partner.broadcast"].sudo().start_broadcast_for_partner(partner, vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)

        return json_response({"broadcast": broadcast.serialize_for_portal()}, status=201)

    @http.route("/api/portal/broadcasts", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_broadcasts(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()

        domain = [("partner_id", "=", partner.id)]
        source = (kwargs.get("source") or "").strip()
        if source in ("manual", "birthday_auto"):
            domain.append(("source", "=", source))

        limit = self._parse_int(kwargs.get("limit")) or 20
        offset = self._parse_int(kwargs.get("offset")) or 0

        model = request.env["partner.broadcast"].sudo()
        broadcasts = model.search(domain, limit=limit, offset=offset, order="create_date desc")
        return json_response({
            "broadcasts": [b.serialize_for_portal() for b in broadcasts],
            "total": model.search_count(domain),
        })

    @http.route("/api/portal/broadcasts/active", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_active_broadcast(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        return json_response({
            "broadcast": request.env["partner.broadcast"].sudo().get_active_broadcast_for_partner(partner),
        })

    @http.route("/api/portal/broadcasts/<int:broadcast_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_broadcast(self, broadcast_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        broadcast = request.env["partner.broadcast"].sudo().search([
            ("id", "=", broadcast_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not broadcast:
            return json_response({"error": "not_found", "message": "ไม่พบ Broadcast ดังกล่าว"}, status=404)

        data = broadcast.serialize_for_portal()
        if str(kwargs.get("recipients") or "").lower() in ("1", "true", "yes"):
            limit = self._parse_int(kwargs.get("limit")) or 50
            offset = self._parse_int(kwargs.get("offset")) or 0
            recipient_model = request.env["partner.broadcast.recipient"].sudo()
            rdomain = [("broadcast_id", "=", broadcast.id)]
            recipients = recipient_model.search(rdomain, limit=limit, offset=offset, order="id")
            data["recipients"] = [r.serialize_for_portal() for r in recipients]
            data["recipients_total"] = recipient_model.search_count(rdomain)
        return json_response({"broadcast": data})

    @http.route("/api/portal/broadcasts/<int:broadcast_id>/cancel", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def cancel_broadcast(self, broadcast_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        broadcast = request.env["partner.broadcast"].sudo().search([
            ("id", "=", broadcast_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not broadcast:
            return json_response({"error": "not_found", "message": "ไม่พบ Broadcast ดังกล่าว"}, status=404)
        broadcast.action_cancel()
        return json_response({"broadcast": broadcast.serialize_for_portal()})

    # ------------------------------------------------------------------
    # Birthday reward config
    # ------------------------------------------------------------------
    @http.route("/api/portal/birthday-reward", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_birthday_reward(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        rule = request.env["partner.birthday.reward"].sudo().with_context(active_test=False).search([
            ("partner_id", "=", partner.id),
        ], order="active desc, create_date desc", limit=1)
        return json_response({"birthday_reward": rule.serialize_for_portal() if rule else False})

    @http.route("/api/portal/birthday-reward", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def upsert_birthday_reward(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()

        vals = {field: payload[field] for field in BIRTHDAY_REWARD_FIELDS if field in payload}
        if "active" in vals:
            vals["active"] = bool(vals["active"])
        if vals.get("min_membership_days") not in (None, ""):
            parsed = self._parse_int(vals["min_membership_days"])
            if parsed is None:
                return json_response(
                    {"error": "validation_error", "message": "min_membership_days ต้องเป็นตัวเลข"},
                    status=400,
                )
            vals["min_membership_days"] = parsed
        else:
            vals.pop("min_membership_days", None)

        coupon_id = payload.get("coupon_id")
        if coupon_id is not None:
            coupon = request.env["partner.coupon"].sudo().search([
                ("id", "=", self._parse_int(coupon_id)),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not coupon:
                return json_response({"error": "coupon_not_found", "message": "ไม่พบคูปองดังกล่าว"}, status=404)
            vals["coupon_id"] = coupon.id

        model = request.env["partner.birthday.reward"].sudo().with_context(active_test=False)
        rule = model.search([("partner_id", "=", partner.id)], order="active desc, create_date desc", limit=1)
        try:
            if rule:
                rule.write(vals)
            else:
                vals["partner_id"] = partner.id
                vals.setdefault("name", "Birthday Reward")
                rule = model.create(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)

        return json_response({"birthday_reward": rule.serialize_for_portal()}, status=201)

    @http.route("/api/portal/birthday-reward/disable", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def disable_birthday_reward(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        rule = request.env["partner.birthday.reward"].sudo().search([
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], limit=1)
        if rule:
            rule.active = False
        return json_response({"birthday_reward": rule.serialize_for_portal() if rule else False})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _broadcast_vals_from_payload(self, partner, payload, require_name):
        vals = {field: payload[field] for field in BROADCAST_CONTENT_FIELDS if field in payload}
        vals.update({field: payload[field] for field in BROADCAST_SEGMENT_FIELDS if field in payload})
        vals["partner_id"] = partner.id

        for bool_field in ("include_coupon", "dry_run"):
            if bool_field in vals:
                vals[bool_field] = bool(vals[bool_field])
        for int_field in ("seg_age_min", "seg_age_max"):
            if vals.get(int_field) in (None, ""):
                vals.pop(int_field, None)
            elif int_field in vals:
                parsed = self._parse_int(vals[int_field])
                if parsed is None:
                    raise ValidationError(f"{int_field} ต้องเป็นตัวเลข")
                vals[int_field] = parsed

        if require_name and not (vals.get("name") or "").strip():
            raise ValidationError("กรุณาระบุชื่อ Broadcast")

        if payload.get("coupon_id"):
            coupon = request.env["partner.coupon"].sudo().search([
                ("id", "=", self._parse_int(payload.get("coupon_id"))),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not coupon:
                raise ValidationError("ไม่พบคูปองดังกล่าว")
            vals["coupon_id"] = coupon.id

        tier_ids = payload.get("seg_tier_ids")
        if tier_ids:
            if not isinstance(tier_ids, list):
                raise ValidationError("seg_tier_ids ต้องเป็น array")
            tiers = request.env["partner.tier"].sudo().search([
                ("id", "in", [self._parse_int(t) for t in tier_ids]),
                ("partner_id", "=", partner.id),
            ])
            if len(tiers) != len(tier_ids):
                raise ValidationError("มี Tier ที่ไม่อยู่ภายใต้ Partner นี้")
            vals["seg_tier_ids"] = [(6, 0, tiers.ids)]

        return vals

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
