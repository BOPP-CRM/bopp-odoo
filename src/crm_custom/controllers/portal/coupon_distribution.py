import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

RULE_FIELDS = (
    "name",
    "active",
    "trigger",
    "schedule_interval_days",
    "seg_gender",
    "seg_age_min",
    "seg_age_max",
    "seg_signup_from",
    "seg_signup_to",
    "seg_email_verified",
    "seg_phone_verified",
    "notify_line",
    "message_type",
    "message_text",
    "flex_payload",
)
RULE_BOOL_FIELDS = ("active", "notify_line")
RULE_INT_FIELDS = ("schedule_interval_days", "seg_age_min", "seg_age_max")
RULE_DATETIME_FIELDS = ("seg_signup_from", "seg_signup_to")
ROUTE = "/api/portal/coupon-distribution-rules"


class PortalCouponDistributionController(http.Controller):
    @http.route(ROUTE, type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_rules(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        rules = request.env["partner.coupon.distribution.rule"].sudo().with_context(active_test=False).search(
            [("partner_id", "=", partner.id)], order="create_date desc"
        )
        return json_response({"rules": [rule.serialize_for_portal() for rule in rules]})

    @http.route(ROUTE, type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_rule(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        payload = self._parse_json_payload()
        try:
            vals = self._rule_vals_from_payload(partner, payload)
            if not vals.get("name"):
                raise ValidationError("กรุณาระบุชื่อกฎ")
            if not vals.get("coupon_id"):
                raise ValidationError("กรุณาเลือกคูปอง")
            rule = request.env["partner.coupon.distribution.rule"].sudo().create(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response({"rule": rule.serialize_for_portal()}, status=201)

    @http.route(ROUTE + "/preview", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def preview_rule(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        payload = self._parse_json_payload()
        model = request.env["partner.coupon.distribution.rule"].sudo()
        try:
            vals = self._rule_vals_from_payload(partner, payload)
            origin = None
            if payload.get("rule_id"):
                origin = self._get_rule(partner, payload["rule_id"])
                if not origin:
                    return self._not_found()
            draft = model.new(vals, origin=origin) if origin else model.new(vals)
            preview = draft.preview_audience()
        except ValidationError as error:
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response({
            "count": preview["count"],
            "sample": preview["sample"],
            "coupon_pool": {
                "available": preview["coupon_available"],
                "sufficient": preview["coupon_available"] >= preview["count"],
            } if vals.get("coupon_id") else False,
        })

    @http.route(ROUTE + "/<int:rule_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_rule(self, rule_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        rule = self._get_rule(portal_user.crm_partner_id.sudo(), rule_id)
        if not rule:
            return self._not_found()
        return json_response({"rule": rule.serialize_for_portal()})

    @http.route(ROUTE + "/<int:rule_id>", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_rule(self, rule_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        partner = portal_user.crm_partner_id.sudo()
        rule = self._get_rule(partner, rule_id)
        if not rule:
            return self._not_found()
        try:
            rule.write(self._rule_vals_from_payload(partner, self._parse_json_payload()))
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response({"rule": rule.serialize_for_portal()})

    @http.route(ROUTE + "/<int:rule_id>", type="http", auth="public", methods=["DELETE"], csrf=False, cors="*")
    def delete_rule(self, rule_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        rule = self._get_rule(portal_user.crm_partner_id.sudo(), rule_id)
        if not rule:
            return self._not_found()
        if rule._has_active_job():
            return json_response(
                {"error": "validation_error", "message": "กฎนี้กำลังแจกคูปองอยู่ ไม่สามารถลบได้"}, status=400
            )
        rule.unlink()
        return json_response({"deleted": True})

    @http.route(ROUTE + "/<int:rule_id>/run", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def run_rule(self, rule_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        rule = self._get_rule(portal_user.crm_partner_id.sudo(), rule_id)
        if not rule:
            return self._not_found()
        try:
            broadcast = rule._run("rule_manual")
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response({"error": "validation_error", "message": str(error)}, status=400)
        return json_response({
            "broadcast": broadcast.serialize_for_portal() if broadcast else False,
            "message": None if broadcast else "ไม่มีสมาชิกใหม่ที่เข้าเงื่อนไข",
            "rule": rule.serialize_for_portal(),
        })

    @http.route(ROUTE + "/<int:rule_id>/runs", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_rule_runs(self, rule_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error
        rule = self._get_rule(portal_user.crm_partner_id.sudo(), rule_id)
        if not rule:
            return self._not_found()
        limit = self._parse_int(kwargs.get("limit")) or 20
        offset = self._parse_int(kwargs.get("offset")) or 0
        model = request.env["partner.broadcast"].sudo()
        domain = [("distribution_rule_id", "=", rule.id)]
        runs = model.search(domain, limit=limit, offset=offset, order="create_date desc")
        return json_response({
            "runs": [run.serialize_for_portal() for run in runs],
            "total": model.search_count(domain),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_rule(self, partner, rule_id):
        rule_id = self._parse_int(rule_id)
        if not rule_id:
            return request.env["partner.coupon.distribution.rule"]
        return request.env["partner.coupon.distribution.rule"].sudo().with_context(active_test=False).search([
            ("id", "=", rule_id),
            ("partner_id", "=", partner.id),
        ], limit=1)

    @staticmethod
    def _not_found():
        return json_response({"error": "not_found", "message": "ไม่พบกฎดังกล่าว"}, status=404)

    def _rule_vals_from_payload(self, partner, payload):
        vals = {field: payload[field] for field in RULE_FIELDS if field in payload}
        vals["partner_id"] = partner.id

        for bool_field in RULE_BOOL_FIELDS:
            if bool_field in vals:
                vals[bool_field] = bool(vals[bool_field])
        for int_field in RULE_INT_FIELDS:
            if vals.get(int_field) in (None, ""):
                vals.pop(int_field, None)
            elif int_field in vals:
                parsed = self._parse_int(vals[int_field])
                if parsed is None:
                    raise ValidationError(f"{int_field} ต้องเป็นตัวเลข")
                vals[int_field] = parsed
        for dt_field in RULE_DATETIME_FIELDS:
            if dt_field in vals and not vals[dt_field]:
                vals[dt_field] = False
        if "name" in vals:
            vals["name"] = (vals["name"] or "").strip()

        if payload.get("coupon_id"):
            coupon = request.env["partner.coupon"].sudo().search([
                ("id", "=", self._parse_int(payload.get("coupon_id"))),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not coupon:
                raise ValidationError("ไม่พบคูปองดังกล่าว")
            vals["coupon_id"] = coupon.id

        if "seg_tier_ids" in payload:
            tier_ids = payload.get("seg_tier_ids") or []
            if not isinstance(tier_ids, list):
                raise ValidationError("seg_tier_ids ต้องเป็น array")
            parsed_ids = [self._parse_int(tier) for tier in tier_ids]
            tiers = request.env["partner.tier"].sudo().search([
                ("id", "in", parsed_ids),
                ("partner_id", "=", partner.id),
            ])
            if len(tiers) != len(set(parsed_ids)):
                raise ValidationError("มี Tier ที่ไม่อยู่ภายใต้ Partner นี้")
            vals["seg_tier_ids"] = [(6, 0, tiers.ids)]

        return vals

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _parse_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
