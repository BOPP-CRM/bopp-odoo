import json

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request
from psycopg2 import IntegrityError

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

ALLOWED_TIER_FIELDS = (
    "name",
    "code",
    "color",
    "icon",
    "convert_points",
    "min_spending",
    "max_spending",
    "is_show_in_ui",
)

ALLOWED_REWARD_FIELDS = (
    "sequence",
    "reward_type",
    "point_value",
    "point_currency_id",
    "coupon_id",
)


class PortalTiersController(http.Controller):
    @http.route("/api/portal/tiers", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_tiers(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        tiers = request.env["partner.tier"].sudo().search([
            ("partner_id", "=", partner.id),
        ], order="min_spending asc, id asc")

        return json_response({
            "join_rewards": [
                self._serialize_reward(reward)
                for reward in partner.join_reward_ids
            ],
            "tiers": [self._serialize_tier(tier) for tier in tiers],
        })

    @http.route("/api/portal/tiers/join-rewards", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_join_rewards(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        if "join_rewards" not in payload:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาส่ง join_rewards"},
                status=400,
            )

        try:
            self._sync_rewards(
                partner,
                payload.get("join_rewards"),
                "join",
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "reward_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "join_rewards": [
                self._serialize_reward(reward)
                for reward in partner.join_reward_ids
            ],
        })

    @http.route("/api/portal/tiers/join-rewards", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_join_rewards(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        return json_response({
            "join_rewards": [
                self._serialize_reward(reward)
                for reward in partner.join_reward_ids
            ],
        })

    @http.route("/api/portal/tiers/<int:tier_id>/rewards", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_tier_rewards(self, tier_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        tier_response = self._get_tier(user.crm_partner_id, tier_id)
        if tier_response["error"]:
            return tier_response["error"]

        tier = tier_response["tier"]
        return json_response({
            "tier": {
                "id": tier.id,
                "code": tier.code,
                "name": tier.name,
            },
            "rewards": [
                self._serialize_reward(reward)
                for reward in tier.promotion_reward_ids
            ],
        })

    @http.route("/api/portal/tiers/<int:tier_id>/rewards", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_tier_rewards(self, tier_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        tier_response = self._get_tier(partner, tier_id)
        if tier_response["error"]:
            return tier_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        rewards_payload = self._get_rewards_from_payload(payload)
        if rewards_payload is None:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาส่ง rewards หรือ promotion_rewards"},
                status=400,
            )

        try:
            self._sync_rewards(
                partner,
                rewards_payload,
                "tier_promotion",
                tier=tier_response["tier"],
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "reward_not_allowed", "message": str(error)},
                status=400,
            )

        tier = tier_response["tier"]
        return json_response({
            "tier": {
                "id": tier.id,
                "code": tier.code,
                "name": tier.name,
            },
            "rewards": [
                self._serialize_reward(reward)
                for reward in tier.promotion_reward_ids
            ],
        })

    @http.route("/api/portal/tiers/<int:tier_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_tier(self, tier_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        tier_response = self._get_tier(user.crm_partner_id, tier_id)
        if tier_response["error"]:
            return tier_response["error"]

        return json_response({
            "tier": self._serialize_tier(tier_response["tier"]),
        })

    @http.route("/api/portal/tiers", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_tier(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = self._extract_tier_vals(payload)
        vals["partner_id"] = partner.id
        rewards_payload = self._get_rewards_from_payload(payload)

        try:
            tier = request.env["partner.tier"].sudo().create(vals)
            if rewards_payload is not None:
                self._sync_rewards(
                    partner,
                    rewards_payload,
                    "tier_promotion",
                    tier=tier,
                )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "tier_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {
                    "error": "tier_not_allowed",
                    "message": "ข้อมูล Tier ไม่ถูกต้อง (code อาจซ้ำ หรือไม่ได้กรอกข้อมูลที่จำเป็น)",
                },
                status=400,
            )

        return json_response({
            "tier": self._serialize_tier(tier),
        }, status=201)

    @http.route("/api/portal/tiers/<int:tier_id>", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_tier(self, tier_id, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        tier_response = self._get_tier(partner, tier_id)
        if tier_response["error"]:
            return tier_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = self._extract_tier_vals(payload)
        rewards_payload = self._get_rewards_from_payload(payload)
        if not vals and rewards_payload is None:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            if vals:
                tier_response["tier"].sudo().write(vals)
            if rewards_payload is not None:
                self._sync_rewards(
                    partner,
                    rewards_payload,
                    "tier_promotion",
                    tier=tier_response["tier"],
                )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "tier_not_allowed", "message": str(error)},
                status=400,
            )
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {
                    "error": "tier_not_allowed",
                    "message": "ข้อมูล Tier ไม่ถูกต้อง (code อาจซ้ำ หรือไม่ได้กรอกข้อมูลที่จำเป็น)",
                },
                status=400,
            )

        return json_response({
            "tier": self._serialize_tier(tier_response["tier"]),
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

    def _extract_tier_vals(self, payload):
        return {
            field: payload[field]
            for field in ALLOWED_TIER_FIELDS
            if field in payload
        }

    def _get_rewards_from_payload(self, payload):
        if "rewards" in payload:
            return payload.get("rewards")
        if "promotion_rewards" in payload:
            return payload.get("promotion_rewards")
        return None

    def _get_tier(self, partner, tier_id):
        tier = request.env["partner.tier"].sudo().search([
            ("id", "=", tier_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not tier:
            return {
                "tier": False,
                "error": json_response(
                    {"error": "tier_not_found", "message": "ไม่พบ Tier ดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "tier": tier,
            "error": False,
        }

    def _sync_rewards(self, partner, rewards_payload, event, tier=None):
        if rewards_payload is None:
            return
        if not isinstance(rewards_payload, list):
            raise ValidationError("รูปแบบ rewards ต้องเป็น array")

        if event == "join":
            existing = partner.join_reward_ids
        else:
            if not tier:
                raise ValidationError("ต้องระบุ Tier สำหรับ promotion rewards")
            existing = tier.promotion_reward_ids

        reward_model = request.env["partner.member.reward"].sudo()
        kept_ids = set()

        for index, item in enumerate(rewards_payload):
            if not isinstance(item, dict):
                raise ValidationError("แต่ละ reward ต้องเป็น object")

            vals = {
                field: item[field]
                for field in ALLOWED_REWARD_FIELDS
                if field in item
            }
            if "reward_type" not in vals:
                raise ValidationError("กรุณาระบุ reward_type")

            vals["event"] = event
            vals["partner_id"] = partner.id
            if tier:
                vals["tier_id"] = tier.id
            if "sequence" not in vals:
                vals["sequence"] = (index + 1) * 10

            self._validate_reward_vals(partner, vals)

            reward_id = item.get("id")
            if reward_id:
                reward = existing.filtered(lambda record: record.id == reward_id)
                if reward:
                    reward.write(vals)
                    kept_ids.add(reward.id)
                    continue

            new_reward = reward_model.create(vals)
            kept_ids.add(new_reward.id)

        rewards_to_remove = existing.filtered(lambda record: record.id not in kept_ids)
        if rewards_to_remove:
            rewards_to_remove.unlink()

    def _validate_reward_vals(self, partner, vals):
        reward_type = vals.get("reward_type")
        if reward_type == "point":
            if vals.get("point_value", 0) <= 0:
                raise ValidationError("Point value ต้องมากกว่า 0")
            currency_id = vals.get("point_currency_id")
            if not currency_id:
                raise ValidationError("กรุณาระบุ point_currency_id")
            currency = request.env["crm.partner.currency"].sudo().search([
                ("id", "=", currency_id),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not currency:
                raise ValidationError("ไม่พบ Point currency ดังกล่าว")
        elif reward_type == "coupon":
            coupon_id = vals.get("coupon_id")
            if not coupon_id:
                raise ValidationError("กรุณาระบุ coupon_id")
            coupon = request.env["partner.coupon"].sudo().search([
                ("id", "=", coupon_id),
                ("partner_id", "=", partner.id),
            ], limit=1)
            if not coupon:
                raise ValidationError("ไม่พบ Coupon ดังกล่าว")
        else:
            raise ValidationError("reward_type ต้องเป็น point หรือ coupon")

    def _serialize_tier(self, tier):
        rewards = [
            self._serialize_reward(reward)
            for reward in tier.promotion_reward_ids
        ]
        return {
            "id": tier.id,
            "code": tier.code,
            "name": tier.name,
            "color": tier.color,
            "image_url": tier.icon or False,
            "convert_points": tier.convert_points,
            "min_spending": tier.min_spending,
            "max_spending": tier.max_spending,
            "is_show_in_ui": tier.is_show_in_ui,
            "rewards": rewards,
            "promotion_rewards": rewards,
        }

    def _serialize_reward(self, reward):
        data = {
            "id": reward.id,
            "sequence": reward.sequence,
            "event": reward.event,
            "reward_type": reward.reward_type,
            "tier_id": reward.tier_id.id if reward.tier_id else False,
        }
        if reward.reward_type == "point":
            data.update({
                "point_value": reward.point_value,
                "point_currency_id": reward.point_currency_id.id,
                "point_currency_name": reward.point_currency_id.name,
            })
        else:
            data.update({
                "coupon_id": reward.coupon_id.id,
                "coupon_name": reward.coupon_id.name,
                "coupon_value": reward.coupon_id.value,
                "coupon_image_url": reward.coupon_id.image or False,
            })
        return data
