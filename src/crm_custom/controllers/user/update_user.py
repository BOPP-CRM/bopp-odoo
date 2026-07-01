import json
from datetime import datetime

from odoo import http
from odoo.http import request

from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response
from .get_user_info import GetOrCreateUserController


class UpdateUserController(http.Controller):

    @http.route("/api/partner/<string:slug>/user", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_user_info(self, slug, **kwargs):
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

        user = request.env["crm.user"].search(
            [
                ("line_user_id", "=", line_profile["userId"]),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if not user:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        update_vals = {}

        display_name = payload.get("display_name")
        if display_name:
            update_vals["display_name"] = display_name
        elif line_profile.get("displayName"):
            update_vals["display_name"] = line_profile["displayName"]

        if "birth_date" in payload:
            birth_date = payload.get("birth_date")
            if not birth_date:
                update_vals["birth_date"] = False
            else:
                try:
                    datetime.strptime(birth_date, "%Y-%m-%d")
                except (TypeError, ValueError):
                    return json_response(
                        {
                            "error": "invalid_birth_date",
                            "message": "birth_date must be in yyyy-mm-dd format.",
                        },
                        status=400,
                    )
                update_vals["birth_date"] = birth_date

        if "gender" in payload:
            update_vals["gender"] = payload.get("gender") or False

        if "address" in payload:
            update_vals["address"] = payload.get("address") or False

        update_vals["is_updated_user_info"] = True

        if update_vals:
            user.sudo().write(update_vals)
            user = request.env["crm.user"].sudo().browse(user.id)

        serializer = GetOrCreateUserController()
        return json_response(serializer._serialize_user_response(user, partner))
