import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response


class PortalZortoutController(http.Controller):
    DEFAULT_LOG_LIMIT = 20
    MAX_LOG_LIMIT = 100
    @http.route(
        "/api/portal/zortout",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_zortout_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        return json_response({
            "zortout": partner.serialize_zortout_status(),
        })

    @http.route(
        "/api/portal/zortout/enable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def enable_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.enable_zortout_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout integration enabled.",
        }, status=201)

    @http.route(
        "/api/portal/zortout/disable",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def disable_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        status = partner.disable_zortout_for_api()
        return json_response({
            "zortout": status,
        })

    @http.route(
        "/api/portal/zortout/regenerate-keys",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def regenerate_zortout_keys(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.regenerate_zortout_keys_for_api()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout keys regenerated and webhook synced.",
        })

    @http.route(
        "/api/portal/zortout/connect",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def connect_zortout(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.connect_zortout_for_api(
                payload.get("storename"),
                payload.get("apikey"),
                payload.get("apisecret"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout webhook configured successfully.",
        }, status=201)

    @http.route(
        "/api/portal/zortout/sync-webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def sync_zortout_webhook(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        try:
            status = partner.resync_zortout_webhook_for_api(
                storename=payload.get("storename"),
                apikey=payload.get("apikey"),
                apisecret=payload.get("apisecret"),
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "zortout": status,
            "message": "Zortout webhook synced successfully.",
        })

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @http.route(
        "/api/portal/zortout/logs",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_zortout_logs(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        limit = self._parse_limit(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        log_model = request.env["partner.zortout.webhook.log"].sudo()
        logs, total = log_model.search_for_portal(partner, limit=limit, offset=offset)

        return json_response({
            "logs": [log.serialize_for_portal() for log in logs],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @http.route(
        "/api/portal/zortout/members/sync",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def start_zortout_member_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload = self._parse_json_payload()
        partner = portal_user.crm_partner_id.sudo()
        user_ids = payload.get("user_ids")
        if user_ids is not None and not isinstance(user_ids, list):
            return json_response(
                {"error": "invalid_payload", "message": "user_ids must be an array."},
                status=400,
            )

        parsed_user_ids = None
        if user_ids:
            parsed_user_ids = []
            for user_id in user_ids:
                try:
                    parsed_user_ids.append(int(user_id))
                except (TypeError, ValueError):
                    return json_response(
                        {"error": "invalid_payload", "message": "user_ids must contain integers."},
                        status=400,
                    )

        job_model = request.env["partner.zortout.member.sync.job"].sudo()
        try:
            job = job_model.start_sync_for_partner(partner, parsed_user_ids)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "job": job.serialize_for_portal(),
            "message": "Zortout member sync started.",
        }, status=201)

    @http.route(
        "/api/portal/zortout/members/sync/active",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_active_zortout_member_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.zortout.member.sync.job"].sudo()
        return json_response({
            "job": job_model.get_active_job_for_partner(partner),
        })

    @http.route(
        "/api/portal/zortout/members/sync/<int:job_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_zortout_member_sync_job(self, job_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job = request.env["partner.zortout.member.sync.job"].sudo().search([
            ("id", "=", job_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not job:
            return json_response(
                {"error": "job_not_found", "message": "ไม่พบงาน sync ดังกล่าว"},
                status=404,
            )

        return json_response({
            "job": job.serialize_for_portal(),
        })

    @http.route(
        "/api/portal/users/<int:user_id>/zortout/sync",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def sync_user_to_zortout(self, user_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        user = request.env["crm.user"].sudo().search([
            ("id", "=", user_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        try:
            result = partner.sync_member_to_zortout(user)
        except ValidationError as error:
            request.env.cr.rollback()
            user = request.env["crm.user"].sudo().browse(user.id)
            return json_response(
                {
                    "error": "sync_failed",
                    "message": str(error),
                    "user": self._serialize_user_zortout(user),
                },
                status=400,
            )

        user = request.env["crm.user"].sudo().browse(user.id)
        return json_response({
            "result": result,
            "user": self._serialize_user_zortout(user),
            "message": "Sync สมาชิกไป Zortout สำเร็จ",
        })

    def _serialize_user_zortout(self, user):
        return {
            "contact_id": user.zortout_contact_id or False,
            "synced_at": fields.Datetime.to_string(user.zortout_synced_at)
            if user.zortout_synced_at
            else False,
            "sync_status": user.zortout_sync_status or False,
            "sync_error": user.zortout_sync_error or False,
        }

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_limit(self, value):
        parsed = self._parse_int(value)
        if parsed is None:
            return self.DEFAULT_LOG_LIMIT
        return min(max(parsed, 1), self.MAX_LOG_LIMIT)
