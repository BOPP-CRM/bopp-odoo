import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import (
    get_portal_admin_from_request,
    get_portal_role,
)
from ....util.request import json_response


class PortalTeamController(http.Controller):
    @http.route("/api/portal/team", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_team_users(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        domain = [
            ("is_partner_portal", "=", True),
            ("crm_partner_id", "=", portal_user.crm_partner_id.id),
        ]

        active = kwargs.get("active")
        if active is not None:
            if str(active).lower() in {"1", "true", "yes"}:
                domain.append(("active", "=", True))
            elif str(active).lower() in {"0", "false", "no"}:
                domain.append(("active", "=", False))

        search_term = (kwargs.get("search") or "").strip()
        if search_term:
            domain += [
                "|",
                ("name", "ilike", search_term),
                ("email", "ilike", search_term),
            ]

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        user_model = request.env["res.users"].sudo()
        if active is None or str(active).lower() in {"0", "false", "no"}:
            user_model = user_model.with_context(active_test=False)

        team_users = user_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="create_date desc, id desc",
        )
        total = user_model.search_count(domain)

        return json_response({
            "team_users": [
                self._serialize_team_user(user)
                for user in team_users
            ],
            "total": total,
        })

    @http.route("/api/portal/team", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_team_user(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        name = (payload.get("name") or "").strip()
        email = payload.get("email")
        password = payload.get("password") or ""

        if not name or not email or not password:
            return json_response(
                {"error": "invalid_request", "message": "name, email and password are required."},
                status=400,
            )
        if len(password) < 8:
            return json_response(
                {"error": "invalid_request", "message": "Password must be at least 8 characters."},
                status=400,
            )

        try:
            team_user = request.env["res.users"].create_partner_portal_user(
                portal_user.crm_partner_id,
                name,
                email,
                password,
                portal_role=payload.get("role"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "team_user_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "team_user": self._serialize_team_user(team_user),
        }, status=201)

    @http.route(
        "/api/portal/team/<int:user_id>",
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
        cors="*",
    )
    def update_team_user(self, user_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        team_user = request.env["res.users"].sudo().with_context(active_test=False).search([
            ("id", "=", user_id),
            ("is_partner_portal", "=", True),
            ("crm_partner_id", "=", portal_user.crm_partner_id.id),
        ], limit=1)
        if not team_user:
            return json_response(
                {"error": "team_user_not_found", "message": "ไม่พบ portal user ดังกล่าว"},
                status=404,
            )

        if not any([
            payload.get("name") is not None,
            payload.get("role") is not None,
            payload.get("active") is not None,
            payload.get("password"),
        ]):
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            team_user.update_partner_portal_user(
                name=payload.get("name"),
                portal_role=payload.get("role"),
                active=payload.get("active"),
                password=payload.get("password"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "team_user_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "team_user": self._serialize_team_user(team_user),
        })

    @http.route("/api/portal/team/invites", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_invites(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        domain = [("partner_id", "=", portal_user.crm_partner_id.id)]

        state = (kwargs.get("state") or "pending").strip().lower()
        if state != "all":
            domain.append(("state", "=", state))

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        invite_model = request.env["partner.portal.invite"].sudo()
        invites = invite_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="create_date desc, id desc",
        )
        total = invite_model.search_count(domain)

        return json_response({
            "invites": [self._serialize_invite(invite) for invite in invites],
            "total": total,
        })

    @http.route("/api/portal/team/invites", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_invite(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        email = payload.get("email")
        name = (payload.get("name") or "").strip()
        if not email:
            return json_response(
                {"error": "invalid_request", "message": "email is required."},
                status=400,
            )

        try:
            invite = request.env["partner.portal.invite"].create_invite(
                portal_user.crm_partner_id,
                portal_user,
                name or email,
                email,
                portal_role=payload.get("role"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "invite_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "invite": self._serialize_invite(invite),
        }, status=201)

    @http.route("/api/portal/team/invites/<int:invite_id>", type="http", auth="public", methods=["DELETE"], csrf=False, cors="*")
    def cancel_invite(self, invite_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        invite = request.env["partner.portal.invite"].sudo().search([
            ("id", "=", invite_id),
            ("partner_id", "=", portal_user.crm_partner_id.id),
        ], limit=1)
        if not invite:
            return json_response(
                {"error": "invite_not_found", "message": "ไม่พบ invite ดังกล่าว"},
                status=404,
            )

        try:
            invite.action_cancel()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "invite_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "invite": self._serialize_invite(invite),
        })

    @http.route("/api/portal/invite/<string:token>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_invite(self, token, **kwargs):
        invite = request.env["partner.portal.invite"].sudo()._get_valid_invite(token)
        if not invite:
            return json_response(
                {"error": "invite_not_found", "message": "Invite link is invalid or expired."},
                status=404,
            )

        partner = invite.partner_id
        return json_response({
            "invite": {
                "email": invite.email,
                "name": invite.name,
                "role": invite.portal_role,
                "expires_at": fields.Datetime.to_string(invite.expires_at),
                "partner": {
                    "name": partner.name,
                    "slug": partner.slug,
                    "logo_url": partner.logo or False,
                },
            },
        })

    @http.route("/api/portal/invite/<string:token>/accept", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def accept_invite(self, token, **kwargs):
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        password = payload.get("password") or ""
        name = (payload.get("name") or "").strip() or None

        invite = request.env["partner.portal.invite"].sudo()._get_valid_invite(token)
        if not invite:
            return json_response(
                {"error": "invite_not_found", "message": "Invite link is invalid or expired."},
                status=404,
            )

        try:
            team_user = invite.accept(password, name=name)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "invite_not_allowed", "message": str(error)},
                status=400,
            )

        portal_token = request.env["partner.portal.token"].sudo().create_for_user(team_user)
        partner = team_user.crm_partner_id

        return json_response({
            "token": portal_token.token,
            "expires_at": fields.Datetime.to_string(portal_token.expires_at),
            "user": {
                "name": team_user.name,
                "email": team_user._get_portal_email(),
                "role": get_portal_role(team_user),
            },
            "partner": {
                "name": partner.name,
                "slug": partner.slug,
                "logo_url": partner.logo or False,
            },
        }, status=201)

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        return payload, None

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _serialize_team_user(self, user):
        return {
            "id": user.id,
            "name": user.name,
            "email": user._get_portal_email(),
            "role": get_portal_role(user),
            "active": user.active,
            "create_date": fields.Datetime.to_string(user.create_date),
        }

    def _serialize_invite(self, invite):
        invited_by = invite.invited_by_id
        return {
            "id": invite.id,
            "name": invite.name,
            "email": invite.email,
            "role": invite.portal_role,
            "state": invite.state,
            "token": invite.token,
            "invite_url": invite.invite_url or False,
            "expires_at": fields.Datetime.to_string(invite.expires_at),
            "accepted_at": fields.Datetime.to_string(invite.accepted_at) if invite.accepted_at else False,
            "invited_by": {
                "id": invited_by.id,
                "name": invited_by.name,
                "email": invited_by._get_portal_email(),
            } if invited_by else False,
            "accepted_user_id": invite.accepted_user_id.id if invite.accepted_user_id else False,
        }
