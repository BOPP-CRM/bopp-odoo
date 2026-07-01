from odoo import fields, http
from odoo.http import request
from ....util.request import json_response
from ....util.line_auth import get_line_profile_from_request


class GetOrCreateUserController(http.Controller):

    @http.route("/api/partner/<string:slug>/user", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_user_info(self, slug, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        partner = request.env["partner"].sudo().search(
            [
                ("slug", "=", slug),
            ],
            limit=1,
        )

        if not partner:
            return json_response(
                {
                    "error": "partner_not_found",
                    "message": "ไม่พบ Client ดังกล่าวโปรดติดต่อเจ้าหน้าที่",
                },
                status=404,
            )

        display_name = line_profile.get("displayName")
        picture_url = line_profile.get("pictureUrl")
        user_id = line_profile.get("userId")
        user = request.env["crm.user"].search(
            [
                ("line_user_id", "=", user_id),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if not user:
            user = request.env["crm.user"].sudo().create({
                "display_name": display_name,
                "picture_url": picture_url,
                "line_user_id": user_id,
                "partner_id": partner.id,
            })

        else:
            user.sudo().write({
                "display_name": display_name,
                "picture_url": picture_url,
            })
            user = request.env["crm.user"].sudo().browse(user.id)

        user._update_tier()
        return json_response(self._serialize_user_response(user, partner))


    def _serialize_user_response(self, user, partner):
        force_verify_phone = not user.is_phone_verified and partner.ui_crm_required_phone
        force_verify_email = not user.is_email_verified and partner.ui_crm_required_email

        has_sms_credit = request.env["crm.otp"].has_sms_otp_credit()
        if not has_sms_credit:
            force_verify_phone = False

        return {
            "display_name": user.display_name,
            "picture_url": user.picture_url,
            "line_user_id": user.line_user_id,
            "email": user.email,
            "phone": user.phone,
            "force_verify_phone": force_verify_phone,
            "force_verify_email": force_verify_email,
            "birth_date": fields.Date.to_string(user.birth_date) if user.birth_date else False,
            "gender": user.gender,
            "address": user.address or False,
            "is_updated_user_info": user.is_updated_user_info,
            "tier": self._serialize_user_tier(user.tier_id),
        }

    def _serialize_user_tier(self, tier):
        if not tier:
            return False

        return {
            "code": tier.code,
            "name": tier.name,
            "min_spending": tier.min_spending,
            "max_spending": tier.max_spending,
            "color": tier.color,
            "image_url": tier.icon or False,
        }
