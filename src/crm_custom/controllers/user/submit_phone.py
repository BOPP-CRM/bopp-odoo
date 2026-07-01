import json
from odoo import http
from odoo.http import request
from ....util.request import json_response


class SubmitPhoneController(http.Controller):

    @http.route("/api/partner/<string:slug>/submit-phone", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def submit_phone(self, slug, **kwargs):
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
                    "message": "ไม่พบ Client โปรดติดต่อผู้ดูแลระบบ",
                },
                status=404,
            )

        user_id = payload.get("userId")
        phone = payload.get("phone")
        user = request.env["crm.user"].search(
            [
                ("line_user_id", "=", user_id),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if user.is_phone_verified and user.phone == phone:
            return json_response({
                    "error": "already_verified_phone",
                    "message": "ไม่สามารถใช้เบอร์โทรเดิมได้",
            },
            status=400,
        )

        if not user:
            return json_response(
                {"error": "user not found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=400,
            )

        user.sudo().write({
            "phone": phone,
            "is_phone_verified": False,
        })

        otp = request.env["crm.otp"].sudo().generate_otp(phone, "phone", "verified_phone", str(user.id), partner)

        return json_response({
            "ref": otp.ref
        })
