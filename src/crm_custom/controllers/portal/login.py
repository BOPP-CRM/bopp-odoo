import json

from odoo import fields, http
from odoo.exceptions import AccessDenied, ValidationError
from odoo.http import request

from ....util.portal_auth import get_authenticated_portal_user, get_portal_role
from ....util.request import json_response


class PortalLoginController(http.Controller):
    @http.route( "/api/portal/login", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def portal_login(self, **kwargs):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        email = (payload.get("email") or payload.get("login") or "").strip().lower()
        password = payload.get("password") or ""
        domain = (payload.get("domain") or payload.get("slug") or "").strip()
        if not email or not password:
            return json_response(
                {"error": "invalid_request", "message": "Email and password are required."},
                status=400,
            )
        if not domain:
            return json_response(
                {"error": "invalid_request", "message": "Domain is required."},
                status=400,
            )

        partner = request.env["partner"].sudo().search([
            ("slug", "=", domain),
        ], limit=1)
        if not partner:
            return json_response(
                {"error": "partner_not_found", "message": "ไม่พบ Partner จาก domain ดังกล่าว"},
                status=404,
            )

        scoped_login = request.env["res.users"]._make_portal_login(partner, email)
        credential = {
            "login": scoped_login,
            "password": password,
            "type": "password",
        }

        try:
            auth_info = request.env["res.users"].sudo().authenticate(
                request.env.cr.dbname,
                credential,
                {"interactive": False},
            )
        except AccessDenied:
            return json_response(
                {"error": "invalid_credentials", "message": "Invalid email or password."},
                status=401,
            )

        user = request.env["res.users"].sudo().browse(auth_info["uid"])

        if not user.is_partner_portal or not user.crm_partner_id:
            return json_response(
                {
                    "error": "portal_access_denied",
                    "message": "This account cannot access the partner portal.",
                },
                status=403,
            )

        if user.crm_partner_id.id != partner.id:
            return json_response(
                {
                    "error": "portal_access_denied",
                    "message": "This account does not belong to the specified partner.",
                },
                status=403,
            )

        internal_group = request.env.ref("base.group_user")
        if internal_group in user.groups_id:
            return json_response(
                {
                    "error": "portal_access_denied",
                    "message": "Internal users must use Odoo login.",
                },
                status=403,
            )

        portal_token = request.env["partner.portal.token"].sudo().create_for_user(user)
        partner = user.crm_partner_id

        return json_response({
            "token": portal_token.token,
            "expires_at": fields.Datetime.to_string(portal_token.expires_at),
            "user": {
                "name": user.name,
                "email": user._get_portal_email(),
                "role": get_portal_role(user),
            },
            "partner": {
                "name": partner.name,
                "slug": partner.slug,
                "logo_url": partner.logo,
                "manual_receipt_require_image": partner.portal_manual_receipt_require_image,
                "warranty_enabled": partner.ui_warranty_enabled,
            },
        })

    @http.route( "/api/portal/me", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def portal_me(self, **kwargs):
        user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        return json_response({
            "user": {
                "name": user.name,
                "email": user._get_portal_email(),
                "role": get_portal_role(user),
            },
            "partner": {
                "name": partner.name,
                "slug": partner.slug,
                "logo_url": partner.logo,
                "manual_receipt_require_image": partner.portal_manual_receipt_require_image,
                "warranty_enabled": partner.ui_warranty_enabled,
            },
        })

    @http.route("/api/portal/me", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_portal_me(self, **kwargs):
        user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        current_password = payload.get("current_password")
        new_password = payload.get("password")
        email = payload.get("email")
        name = payload.get("name")

        if not any([name is not None, email is not None, new_password]):
            return json_response(
                {"error": "invalid_request", "message": "No profile fields to update."},
                status=400,
            )

        try:
            user.sudo().update_portal_account(
                name=name,
                email=email,
                current_password=current_password,
                new_password=new_password,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "profile_not_allowed", "message": str(error)},
                status=400,
            )

        partner = user.crm_partner_id
        return json_response({
            "user": {
                "name": user.name,
                "email": user._get_portal_email(),
                "role": get_portal_role(user),
            },
            "partner": {
                "name": partner.name,
                "slug": partner.slug,
                "logo_url": partner.logo,
                "manual_receipt_require_image": partner.portal_manual_receipt_require_image,
                "warranty_enabled": partner.ui_warranty_enabled,
            },
        })
