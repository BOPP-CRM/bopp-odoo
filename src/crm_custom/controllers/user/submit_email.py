import json
from odoo import http
from odoo.http import request
from ....util.request import json_response


class SubmitEmailController(http.Controller):

    @http.route("/api/partner/<string:slug>/submit-email", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def submit_email(self, slug, **kwargs):
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
                    "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่",
                },
                status=404,
            )

        user_id = payload.get("userId")
        email = payload.get("email")
        user = request.env["crm.user"].search(
            [
                ("line_user_id", "=", user_id),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if user.is_email_verified and user.email == email:
            return json_response({
                    "error": "already_verified_email",
                    "message": "อีเมลห้ามซ้ำกับของเดิม",
            },
            status=400,
        )

        if not user:
            return json_response(
                {"error": "user not found", "message": "ไม่พบผู้ใช้งานดังกล่าวในระบบ"},
                status=400,
            )

        user.sudo().write({
            "email": email,
            "is_email_verified": False,
        })

        otp = request.env["crm.otp"].sudo().generate_otp(email, "email", "verified_email", str(user.id), partner)

        return json_response({
            "ref": otp.ref
        })
