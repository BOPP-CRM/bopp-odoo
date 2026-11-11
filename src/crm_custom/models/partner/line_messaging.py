import json
import logging

import requests

from odoo import fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

LINE_API_BASE_URL = "https://api.line.me"
LINE_MULTICAST_MAX_RECIPIENTS = 500
LINE_MAX_MESSAGES_PER_REQUEST = 5
LINE_REQUEST_TIMEOUT = 30


class LineRateLimited(Exception):
    """Raised when the LINE Messaging API responds with HTTP 429."""


class PartnerLineMessaging(models.Model):
    _inherit = "partner"

    line_messaging_enabled = fields.Boolean(
        string="LINE Messaging Enabled",
        default=False,
        copy=False,
        tracking=True,
    )
    line_messaging_bot_basic_id = fields.Char(
        string="LINE Bot Basic ID",
        copy=False,
        readonly=True,
    )
    line_messaging_verified_at = fields.Datetime(
        string="LINE Messaging Verified At",
        copy=False,
        readonly=True,
    )

    def _ensure_line_messaging_ready(self):
        self.ensure_one()
        if not self._is_line_messaging_ready_safe():
            raise ValidationError(
                "ยังไม่ได้เชื่อมต่อ LINE Messaging API กรุณาตั้งค่า Channel Access Token ก่อน"
            )

    def _is_line_messaging_ready_safe(self):
        self.ensure_one()
        return bool(self.line_messaging_enabled and (self.partner_line_channel_access_token or "").strip())

    def _line_messaging_headers(self):
        self.ensure_one()
        return {
            "Authorization": f"Bearer {(self.partner_line_channel_access_token or '').strip()}",
            "Content-Type": "application/json",
        }

    def _line_verify_token(self, channel_access_token):
        token = (channel_access_token or "").strip()
        if not token:
            raise ValidationError("กรุณาระบุ Channel Access Token")

        try:
            response = requests.get(
                f"{LINE_API_BASE_URL}/v2/bot/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=LINE_REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            _logger.warning("LINE bot/info failed for partner %s: %s", self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ LINE API ได้") from error

        if response.status_code == 401:
            raise ValidationError("Channel Access Token ไม่ถูกต้องหรือหมดอายุ")
        if response.status_code != 200:
            raise ValidationError(
                f"LINE API ตอบกลับผิดพลาด (HTTP {response.status_code})"
            )

        try:
            data = response.json()
        except ValueError as error:
            raise ValidationError("LINE API ตอบกลับข้อมูลไม่ถูกต้อง") from error
        if not isinstance(data, dict):
            raise ValidationError("LINE API ตอบกลับข้อมูลไม่ถูกต้อง")
        return data

    def _line_send(self, path, payload):
        self.ensure_one()
        self._ensure_line_messaging_ready()

        try:
            response = requests.post(
                f"{LINE_API_BASE_URL}{path}",
                headers=self._line_messaging_headers(),
                json=payload,
                timeout=LINE_REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            _logger.warning("LINE %s failed for partner %s: %s", path, self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ LINE API ได้") from error

        if response.status_code == 429:
            raise LineRateLimited(f"LINE rate limit for partner {self.id}")

        if response.status_code not in (200, 202):
            message = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    message = body.get("message") or ""
            except ValueError:
                message = (response.text or "")[:300]
            _logger.warning(
                "LINE %s rejected for partner %s (HTTP %s): %s",
                path,
                self.id,
                response.status_code,
                message,
            )
            raise ValidationError(
                message or f"LINE API ส่งข้อความไม่สำเร็จ (HTTP {response.status_code})"
            )
        return True

    def _line_multicast(self, line_user_ids, messages):
        self.ensure_one()
        recipients = [uid for uid in line_user_ids if uid]
        if not recipients:
            return True
        if len(recipients) > LINE_MULTICAST_MAX_RECIPIENTS:
            raise ValidationError(
                f"multicast รับได้สูงสุด {LINE_MULTICAST_MAX_RECIPIENTS} รายต่อครั้ง"
            )
        return self._line_send(
            "/v2/bot/message/multicast",
            {"to": recipients, "messages": messages[:LINE_MAX_MESSAGES_PER_REQUEST]},
        )

    def _line_push(self, line_user_id, messages):
        self.ensure_one()
        if not line_user_id:
            return True
        return self._line_send(
            "/v2/bot/message/push",
            {"to": line_user_id, "messages": messages[:LINE_MAX_MESSAGES_PER_REQUEST]},
        )

    @staticmethod
    def _build_line_messages(message_type, message_text, flex_payload, image_url=None):
        message_type = (message_type or "text").strip()
        text = (message_text or "").strip()

        if message_type == "flex":
            try:
                contents = json.loads(flex_payload or "")
            except (TypeError, ValueError) as error:
                raise ValidationError("Flex payload ไม่ใช่ JSON ที่ถูกต้อง") from error
            if not isinstance(contents, dict) or contents.get("type") not in ("bubble", "carousel"):
                raise ValidationError("Flex payload ต้องเป็น object ที่มี type = bubble หรือ carousel")
            return [{
                "type": "flex",
                "altText": text or "ข้อความจากร้านค้า",
                "contents": contents,
            }]

        if not text:
            raise ValidationError("กรุณาระบุข้อความ")

        messages = [{"type": "text", "text": text}]
        image_url = (image_url or "").strip()
        if image_url:
            if not image_url.startswith("https://"):
                raise ValidationError("รูปภาพต้องเป็น URL แบบ https")
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            })
        return messages

    def connect_line_messaging_for_api(self, channel_access_token):
        self.ensure_one()
        token = (channel_access_token or "").strip()
        info = self._line_verify_token(token)
        self.write({
            "partner_line_channel_access_token": token,
            "line_messaging_enabled": True,
            "line_messaging_bot_basic_id": info.get("basicId") or False,
            "line_messaging_verified_at": fields.Datetime.now(),
        })
        return self.serialize_line_messaging_status()

    def disable_line_messaging_for_api(self):
        self.ensure_one()
        self.write({"line_messaging_enabled": False})
        return self.serialize_line_messaging_status()

    def send_line_test_message_for_api(self, line_user_id, text):
        self.ensure_one()
        self._ensure_line_messaging_ready()
        line_user_id = (line_user_id or "").strip()
        if not line_user_id:
            raise ValidationError("กรุณาระบุ LINE User ID ปลายทาง")
        text = (text or "").strip() or "ทดสอบการเชื่อมต่อ LINE Messaging API"
        self._line_push(line_user_id, [{"type": "text", "text": text}])
        return {"sent": True}

    def serialize_line_messaging_status(self):
        self.ensure_one()
        token = (self.partner_line_channel_access_token or "").strip()
        return {
            "enabled": bool(self.line_messaging_enabled),
            "configured": bool(token),
            "bot_basic_id": self.line_messaging_bot_basic_id or None,
            "verified_at": (
                fields.Datetime.to_string(self.line_messaging_verified_at)
                if self.line_messaging_verified_at
                else False
            ),
            "token_masked": f"...{token[-4:]}" if len(token) >= 4 else bool(token),
        }
