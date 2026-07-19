import json
import logging

from odoo import http
from odoo.http import request

from ....util.request import json_response

_logger = logging.getLogger(__name__)

SUPPORTED_METHODS = {"ADDORDER", "UPDATEORDER", "DELETEORDER"}


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
        method = (kwargs.get("method") or request.params.get("method") or "").strip().upper()

        if not partner.validate_zortout_request_headers(headers):
            request.env["partner.zortout.webhook.log"].sudo().log_request(
                partner,
                method or "VERIFY",
                http_status=401,
                result={"status": "unauthorized"},
                message="Invalid webhook keys. ตรวจสอบว่า key1 ใน ZORT ตรงกับที่แสดงใน Portal",
            )
            return json_response(
                {"error": "unauthorized", "message": "Invalid webhook keys."},
                status=401,
            )

        if method not in SUPPORTED_METHODS:
            request.env["partner.zortout.webhook.log"].sudo().log_request(
                partner,
                method or "UNKNOWN",
                http_status=400,
                result={"status": "bad_request", "reason": "unsupported_method"},
                message="Unsupported webhook method.",
            )
            return json_response(
                {"error": "bad_request", "message": "Unsupported webhook method."},
                status=400,
            )

        payload = self._extract_payload(kwargs)
        log_model = request.env["partner.zortout.webhook.log"].sudo()

        if payload is None:
            log_model.log_request(
                partner,
                method,
                payload=None,
                http_status=400,
                result={"status": "bad_request", "reason": "invalid_payload"},
                message="Invalid payload.",
            )
            return json_response(
                {"error": "bad_request", "message": "Invalid payload."},
                status=400,
            )

        try:
            result = partner.process_zortout_webhook(method, payload)
        except Exception as error:
            request.env.cr.rollback()
            log_model.log_request(
                partner,
                method,
                payload=payload,
                http_status=500,
                result={"status": "error"},
                message=str(error),
            )
            _logger.exception(
                "Zortout webhook failed for partner %s method %s",
                partner.id,
                method,
            )
            return json_response(
                {"error": "internal_error", "message": "Unable to process webhook."},
                status=500,
            )

        log_model.log_request(
            partner,
            method,
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
