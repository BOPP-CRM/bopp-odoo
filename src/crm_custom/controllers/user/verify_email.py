import json
from odoo import http
from odoo.http import request
from ....util.request import json_response


class VerifyEmailController(http.Controller):

    @http.route("/api/partner/<string:slug>/verify-email", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def verify_email(self, slug, **kwargs):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        ref = payload.get("ref")
        otp = payload.get("otp")
        record = request.env["crm.otp"].search(
            [
                ("ref", "=", ref),
                ("type", "=", "email"),
                ("key", "=", "verified_email"),
            ],
            limit=1,
        )

        if not record:
            return json_response(
                {"error": "invalid_otp", "message": "รหัส OTP ไม่ถูกต้อง"},
                status=400,
            )

        if record.otp != otp:
            return json_response(
                {"error": "invalid_otp", "message": "รหัส OTP ไม่ถูกต้อง"},
                status=400,
            )

        user = request.env["crm.user"].search(
            [
                ("id", "=", record.key_id)
            ],
            limit=1
        )

        if not user:
            return json_response(
                {"error": "not_found_user", "message": "User Not Found"},
                status=400,
            )

        user.sudo().write({
            "is_email_verified": True,
        })

        return json_response(
            {"success": True},
        )
