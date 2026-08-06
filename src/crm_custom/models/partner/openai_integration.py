import json
import logging
import re

import requests

from odoo import fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_RECEIPT_VISION_MODEL = "gpt-4o-mini"

RECEIPT_SALE_AI_PROMPT = """Analyze this receipt image and extract sales transaction data.
Return a single JSON object with exactly this structure:
{
  "order_number": "string or null",
  "order_date": "YYYY-MM-DD or null",
  "discount": number or null,
  "vat_amount": number or null,
  "amount": number,
  "payment_amount": number or null,
  "payment_status": "Paid" or null,
  "customer_name": "string or null",
  "customer_phone": "string or null",
  "customer_email": "string or null",
  "items": [
    {
      "sku": "string or null",
      "name": "string",
      "quantity": number,
      "price_per_unit": number or null,
      "discount": number or null,
      "total_price": number
    }
  ]
}
Rules:
- Use Thai or English text from the receipt as-is for names.
- amount is the final total payable on the receipt.
- If line items exist, include all visible product/service lines.
- Use null for unknown optional fields, not empty strings.
- Return ONLY valid JSON."""


class PartnerOpenaiIntegration(models.Model):
    _inherit = "partner"

    openai_api_key = fields.Char(
        string="OpenAI API Key",
        copy=False,
        password="True",
        tracking=True,
    )

    @staticmethod
    def _mask_openai_api_key(api_key):
        api_key = (api_key or "").strip()
        if not api_key:
            return None
        if len(api_key) <= 8:
            return "****"
        return f"{api_key[:7]}...{api_key[-4:]}"

    def serialize_openai_status(self):
        self.ensure_one()
        return {
            "configured": bool((self.openai_api_key or "").strip()),
            "masked_key": self._mask_openai_api_key(self.openai_api_key),
        }

    def save_openai_api_key_for_api(self, api_key):
        self.ensure_one()
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValidationError("กรุณาระบุ OpenAI API Key")
        if not api_key.startswith("sk-"):
            raise ValidationError("รูปแบบ OpenAI API Key ไม่ถูกต้อง")
        self.write({"openai_api_key": api_key})
        return self.serialize_openai_status()

    def remove_openai_api_key_for_api(self):
        self.ensure_one()
        self.write({"openai_api_key": False})
        return self.serialize_openai_status()

    def _ensure_openai_api_key(self):
        self.ensure_one()
        if not (self.openai_api_key or "").strip():
            raise ValidationError("ยังไม่ได้ตั้งค่า OpenAI API Key")

    @staticmethod
    def _parse_openai_json_content(content):
        content = (content or "").strip()
        if not content:
            raise ValidationError("OpenAI ไม่ได้ส่งข้อมูลกลับมา")

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise ValidationError("OpenAI ตอบกลับข้อมูลที่ไม่ใช่ JSON") from None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as error:
                raise ValidationError("OpenAI ตอบกลับ JSON ไม่ถูกต้อง") from error

    def extract_receipt_sale_data_with_openai(self, receipt):
        self.ensure_one()
        self._ensure_openai_api_key()
        receipt.ensure_one()

        if not receipt.receipt_image:
            raise ValidationError("ใบเสร็จนี้ไม่มีรูปภาพ")

        image_b64 = receipt._fetch_image_from_url(receipt.receipt_image)
        if not image_b64:
            raise ValidationError("ไม่สามารถโหลดรูปใบเสร็จได้")

        if isinstance(image_b64, bytes):
            image_b64 = image_b64.decode("ascii")

        payload = {
            "model": OPENAI_RECEIPT_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECEIPT_SALE_AI_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                            },
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 4096,
        }

        try:
            response = requests.post(
                OPENAI_CHAT_COMPLETIONS_URL,
                headers={
                    "Authorization": f"Bearer {self.openai_api_key.strip()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
        except requests.RequestException as error:
            _logger.warning(
                "OpenAI receipt extraction failed for partner %s receipt %s: %s",
                self.id,
                receipt.id,
                error,
            )
            raise ValidationError("ไม่สามารถเชื่อมต่อ OpenAI API ได้") from error

        try:
            data = response.json()
        except ValueError as error:
            raise ValidationError("OpenAI ตอบกลับข้อมูลไม่ถูกต้อง") from error

        if not response.ok:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else None
            raise ValidationError(message or "OpenAI API request failed")

        choices = data.get("choices") or []
        if not choices:
            raise ValidationError("OpenAI ไม่ได้ส่งผลลัพธ์กลับมา")

        content = choices[0].get("message", {}).get("content")
        parsed = self._parse_openai_json_content(content)
        if not isinstance(parsed, dict):
            raise ValidationError("OpenAI ตอบกลับรูปแบบข้อมูลไม่ถูกต้อง")

        return parsed
