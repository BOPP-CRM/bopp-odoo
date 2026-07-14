import json
import logging

from odoo import http
from odoo.http import request

from ....util.request import json_response

_logger = logging.getLogger(__name__)

SUPPORTED_METHODS = {"ADDORDER", "UPDATEORDER"}


class ZortoutWebhookController(http.Controller):
    @http.route(
        "/api/integrations/zortout/<string:token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def receive_webhook(self, token, **kwargs):
        partner = request.env["partner"].sudo().get_partner_from_zortout_token(token)
        if not partner:
            return json_response(
                {"error": "not_found", "message": "Invalid or disabled webhook."},
                status=404,
            )

        headers = request.httprequest.headers
        if not partner.validate_zortout_request_headers(headers):
            return json_response(
                {"error": "unauthorized", "message": "Invalid webhook keys."},
                status=401,
            )

        method = (kwargs.get("method") or request.params.get("method") or "").strip().upper()
        if method not in SUPPORTED_METHODS:
            return json_response(
                {"error": "bad_request", "message": "Unsupported webhook method."},
                status=400,
            )

        payload = self._extract_payload(kwargs)
        if payload is None:
            return json_response(
                {"error": "bad_request", "message": "Invalid payload."},
                status=400,
            )

        try:
            result = partner.process_zortout_webhook(method, payload)
        except Exception:
            request.env.cr.rollback()
            _logger.exception(
                "Zortout webhook failed for partner %s method %s",
                partner.id,
                method,
            )
            return json_response(
                {"error": "internal_error", "message": "Unable to process webhook."},
                status=500,
            )

        return json_response(result, status=200)

    def _extract_payload(self, kwargs):
        raw_payload = kwargs.get("payload")
        if raw_payload is None:
            raw_payload = request.params.get("payload")

        if raw_payload is None and request.httprequest.data:
            content_type = (request.httprequest.content_type or "").lower()
            if "application/json" in content_type:
                try:
                    body = json.loads(request.httprequest.data.decode("utf-8"))
                except (TypeError, ValueError, UnicodeDecodeError):
                    return None
                if isinstance(body, dict) and "payload" in body:
                    raw_payload = body.get("payload")
                elif isinstance(body, dict):
                    return body

        if isinstance(raw_payload, dict):
            return raw_payload

        if isinstance(raw_payload, str):
            raw_payload = raw_payload.strip()
            if not raw_payload:
                return None
            try:
                parsed = json.loads(raw_payload)
            except (TypeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None

        return None
