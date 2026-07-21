import json
import logging

from odoo import http
from odoo.http import request

from ....util.request import json_response

_logger = logging.getLogger(__name__)


class OmisellWebhookController(http.Controller):
    @http.route(
        "/api/integrations/omisell/<string:token>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def receive_webhook(self, token, **kwargs):
        partner = request.env["partner"].sudo().get_partner_from_omisell_token(token)
        if not partner:
            return json_response(
                {"error": "not_found", "message": "Invalid or disabled webhook."},
                status=404,
            )

        headers = request.httprequest.headers
        log_model = request.env["partner.omisell.webhook.log"].sudo()

        payload = self._extract_payload(kwargs)
        if payload is None:
            log_model.log_request(
                partner,
                payload=None,
                http_status=400,
                result={"status": "bad_request", "reason": "invalid_payload"},
                message="Invalid payload.",
            )
            return json_response(
                {"error": "bad_request", "message": "Invalid payload."},
                status=400,
            )

        if not partner.validate_omisell_request_headers(headers, payload=payload):
            log_model.log_request(
                partner,
                payload=payload,
                http_status=401,
                result={"status": "unauthorized"},
                message="Invalid Omisell webhook headers.",
            )
            return json_response(
                {"error": "unauthorized", "message": "Invalid Omisell webhook headers."},
                status=401,
            )

        try:
            result = partner.process_omisell_webhook(payload)
        except Exception as error:
            request.env.cr.rollback()
            log_model.log_request(
                partner,
                payload=payload,
                http_status=500,
                result={"status": "error"},
                message=str(error),
            )
            _logger.exception(
                "Omisell webhook failed for partner %s request %s",
                partner.id,
                payload.get("request_id"),
            )
            return json_response(
                {"error": "internal_error", "message": "Unable to process webhook."},
                status=500,
            )

        log_model.log_request(
            partner,
            payload=payload,
            http_status=200,
            result=result,
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
                if isinstance(body, dict):
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
