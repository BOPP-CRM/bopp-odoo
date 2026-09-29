import logging
import os
from urllib.parse import urlencode

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)


class IntegrationsLazadaController(http.Controller):
    @http.route(
        "/api/integrations/lazada/oauth/callback",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def lazada_oauth_callback(self, code=None, state=None, **kwargs):
        partner = request.env["partner"].sudo().get_partner_from_lazada_oauth_state(state)
        if not partner:
            return self._result_page(False, "ลิงก์เชื่อมต่อไม่ถูกต้องหรือหมดอายุ กรุณาลองใหม่อีกครั้ง")

        try:
            partner.complete_lazada_oauth(code)
        except ValidationError as error:
            request.env.cr.rollback()
            _logger.warning("Lazada OAuth callback failed for partner %s: %s", partner.id, error)
            return self._result_page(False, str(error))
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Lazada OAuth callback crashed for partner %s", partner.id)
            return self._result_page(False, "เกิดข้อผิดพลาดระหว่างเชื่อมต่อ Lazada")

        return self._result_page(True, "เชื่อมต่อร้าน Lazada สำเร็จ")

    def _result_page(self, success, message):
        portal_base = (os.getenv("PORTAL_FRONTEND_PATH") or "").rstrip("/")
        if portal_base:
            status = "success" if success else "error"
            query = urlencode({"status": status, "message": message})
            return request.redirect(f"{portal_base}/lazada/callback?{query}")

        escaped_message = (
            message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return request.make_response(
            f"<html><body><p>{escaped_message}</p></body></html>",
            headers=[("Content-Type", "text/html; charset=utf-8")],
        )
