import logging

import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .zortout_integration import ZORTOUT_API_BASE_URL

_logger = logging.getLogger(__name__)


class PartnerZortoutContactSync(models.Model):
    _inherit = "partner"

    def _ensure_zortout_member_sync_ready(self):
        self.ensure_one()
        if not self.zortout_enabled:
            raise ValidationError("กรุณา enable Zortout ก่อนทำการ sync สมาชิก")
        if not self.zortout_api_key or not self.zortout_api_secret or not self.zortout_store_name:
            raise ValidationError("กรุณาเชื่อมต่อ Zortout API credentials ก่อนทำการ sync สมาชิก")

    def _zortout_contact_request_headers(self):
        self.ensure_one()
        return self._zortout_request_headers(
            self.zortout_store_name,
            self.zortout_api_key,
            self.zortout_api_secret,
        )

    @staticmethod
    def _parse_zortout_contact_list_response(response):
        try:
            data = response.json()
        except ValueError:
            return False, "Zortout ตอบกลับข้อมูลไม่ถูกต้อง", []

        if not isinstance(data, dict):
            return False, "Zortout ตอบกลับข้อมูลไม่ถูกต้อง", []

        res = data.get("res")
        if res == 200 or str(res) == "200":
            contact_list = data.get("list") or []
            if not isinstance(contact_list, list):
                contact_list = []
            return True, "Success", contact_list

        res_desc = data.get("resDesc") or "Zortout API request failed"
        return False, res_desc, []

    def _zortout_get_contacts(self, keyword, page=1, limit=500):
        self.ensure_one()
        keyword = (keyword or "").strip()
        if len(keyword) < 3:
            return True, "Success", []

        try:
            response = requests.get(
                f"{ZORTOUT_API_BASE_URL}/Contact/GetContacts",
                headers=self._zortout_contact_request_headers(),
                params={
                    "keyword": keyword,
                    "page": page,
                    "limit": limit,
                },
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning("Zortout GetContacts failed for partner %s: %s", self.id, error)
            return False, "ไม่สามารถเชื่อมต่อ Zortout API ได้", []

        return self._parse_zortout_contact_list_response(response)

    def _find_zortout_contact(self, phone, email, contact_code=None):
        self.ensure_one()
        seen_ids = set()
        contacts = []

        keywords = {
            (phone or "").strip(),
            (email or "").strip(),
            (contact_code or "").strip(),
        }
        for keyword in filter(None, keywords):
            if len(keyword) < 3:
                continue
            ok, message, found = self._zortout_get_contacts(keyword)
            if not ok:
                raise ValidationError(message or "ไม่สามารถค้นหา contact ใน Zortout ได้")
            for contact in found:
                contact_id = contact.get("id")
                if contact_id in seen_ids:
                    continue
                seen_ids.add(contact_id)
                contacts.append(contact)

        normalized_phone = self._normalize_zortout_phone(phone)
        normalized_email = (email or "").strip().lower()
        normalized_code = (contact_code or "").strip()

        for contact in contacts:
            contact_phone = self._normalize_zortout_phone(contact.get("phone"))
            contact_email = (contact.get("email") or "").strip().lower()
            contact_code_value = (contact.get("code") or "").strip()
            if normalized_code and contact_code_value == normalized_code:
                return contact
            if normalized_phone and contact_phone == normalized_phone:
                return contact
            if normalized_email and contact_email == normalized_email:
                return contact

        return {}

    @staticmethod
    def _compact_zortout_payload(payload):
        return {
            key: value
            for key, value in payload.items()
            if value not in (None, "", False)
        }

    def _build_zortout_contact_payload(self, user):
        self.ensure_one()
        return self._compact_zortout_payload({
            "code": f"BOPP-{user.id}",
            "name": user.display_name,
            "phone": self._normalize_zortout_phone(user.phone) or (user.phone or "").strip(),
            "email": (user.email or "").strip(),
            "address": (user.address or "").strip(),
        })

    def _zortout_add_contact(self, user):
        self.ensure_one()
        payload = self._build_zortout_contact_payload(user)
        try:
            response = requests.post(
                f"{ZORTOUT_API_BASE_URL}/Contact/AddContact",
                headers=self._zortout_contact_request_headers(),
                json=payload,
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning("Zortout AddContact failed for partner %s: %s", self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ Zortout API ได้") from error

        ok, message, data = self._parse_zortout_response(response)
        if not ok:
            _logger.warning(
                "Zortout AddContact rejected for partner %s user %s: %s",
                self.id,
                user.id,
                data,
            )
            raise ValidationError(message or "ไม่สามารถเพิ่ม contact ใน Zortout ได้")

        contact_id = self._extract_zortout_contact_id(data)
        if not contact_id:
            _logger.warning(
                "Zortout AddContact missing contact id for partner %s user %s: %s",
                self.id,
                user.id,
                data,
            )
            raise ValidationError("Zortout ไม่ได้ส่ง contact id กลับมา")
        return int(contact_id)

    def _zortout_update_contact(self, contact_id, user):
        self.ensure_one()
        payload = self._build_zortout_contact_payload(user)
        try:
            response = requests.post(
                f"{ZORTOUT_API_BASE_URL}/Contact/UpdateContact",
                headers=self._zortout_contact_request_headers(),
                params={"id": int(contact_id)},
                json=payload,
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning("Zortout UpdateContact failed for partner %s: %s", self.id, error)
            raise ValidationError("ไม่สามารถเชื่อมต่อ Zortout API ได้") from error

        ok, message, data = self._parse_zortout_response(response)
        if not ok:
            _logger.warning(
                "Zortout UpdateContact rejected for partner %s contact %s: %s",
                self.id,
                contact_id,
                data,
            )
            raise ValidationError(message or "ไม่สามารถอัปเดต contact ใน Zortout ได้")
        return int(contact_id)

    @api.model
    def _extract_zortout_contact_id(self, data):
        if not isinstance(data, dict):
            return False

        detail = data.get("detail")
        if isinstance(detail, dict) and detail.get("id"):
            return detail.get("id")
        if data.get("id"):
            return data.get("id")
        return False

    def sync_member_to_zortout(self, user):
        self.ensure_one()
        if user.partner_id != self:
            raise ValidationError("สมาชิกไม่ได้อยู่ใน partner นี้")

        self._ensure_zortout_member_sync_ready()

        phone = user.phone
        email = user.email
        if not (phone or "").strip() and not (email or "").strip():
            user.write({
                "zortout_sync_status": "skipped",
                "zortout_sync_error": "ไม่มีเบอร์โทรหรืออีเมลสำหรับค้นหา contact",
                "zortout_synced_at": fields.Datetime.now(),
            })
            raise ValidationError("สมาชิกไม่มีเบอร์โทรหรืออีเมล")

        user.write({"zortout_sync_status": "pending", "zortout_sync_error": False})

        contact_code = f"BOPP-{user.id}"
        existing_contact = self._find_zortout_contact(phone, email, contact_code)
        contact_id = existing_contact.get("id") if existing_contact else False

        try:
            if contact_id:
                contact_id = self._zortout_update_contact(contact_id, user)
                action = "updated"
            elif user.zortout_contact_id:
                contact_id = self._zortout_update_contact(user.zortout_contact_id, user)
                action = "updated"
            else:
                contact_id = self._zortout_add_contact(user)
                action = "created"
        except ValidationError as error:
            user.write({
                "zortout_sync_status": "failed",
                "zortout_sync_error": str(error),
                "zortout_synced_at": fields.Datetime.now(),
            })
            raise

        user.write({
            "zortout_contact_id": int(contact_id),
            "zortout_sync_status": "synced",
            "zortout_sync_error": False,
            "zortout_synced_at": fields.Datetime.now(),
        })
        return {
            "action": action,
            "contact_id": int(contact_id),
        }
