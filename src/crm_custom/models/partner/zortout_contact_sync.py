import json
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

    def _is_zortout_member_sync_enabled(self):
        self.ensure_one()
        return bool(
            self.zortout_enabled
            and self.zortout_api_key
            and self.zortout_api_secret
            and self.zortout_store_name
        )

    def _zortout_contact_request_headers(self):
        self.ensure_one()
        return self._zortout_request_headers(
            self.zortout_store_name,
            self.zortout_api_key,
            self.zortout_api_secret,
        )

    @staticmethod
    def _compact_zortout_payload(payload):
        return {
            key: value
            for key, value in payload.items()
            if value not in (None, "", False)
        }

    @api.model
    def _format_zortout_address(self, address):
        address = (address or "").strip()
        if not address:
            return ""

        try:
            parsed = json.loads(address)
        except (TypeError, ValueError):
            return address

        if not isinstance(parsed, dict):
            return address

        parts = [
            parsed.get("sub_district"),
            parsed.get("district"),
            parsed.get("province"),
            parsed.get("postal_code"),
        ]
        return " ".join(part for part in parts if part)

    def _parse_zortout_contact_list_response(self, response):
        ok, message, data = self._parse_zortout_response(response)
        if isinstance(data, dict) and "list" in data:
            contact_list = data.get("list") or []
            if not isinstance(contact_list, list):
                contact_list = []
            if ok or response.ok:
                return True, message or "Success", contact_list

        if ok:
            return True, message or "Success", []

        _logger.warning(
            "Zortout GetContacts unexpected response (status=%s): %s",
            response.status_code,
            data,
        )
        return False, message or "Zortout API request failed", []

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

    def _zortout_get_contact_detail(self, contact_id):
        self.ensure_one()
        if not contact_id or int(contact_id) <= 0:
            return False, "ไม่พบ contact ใน Zortout", {}

        try:
            response = requests.get(
                f"{ZORTOUT_API_BASE_URL}/Contact/GetContactDetail",
                headers=self._zortout_contact_request_headers(),
                params={"id": int(contact_id)},
                timeout=30,
            )
        except requests.RequestException as error:
            _logger.warning(
                "Zortout GetContactDetail failed for partner %s contact %s: %s",
                self.id,
                contact_id,
                error,
            )
            return False, "ไม่สามารถเชื่อมต่อ Zortout API ได้", {}

        ok, message, data = self._parse_zortout_response(response)
        if isinstance(data, dict) and data.get("id") and (ok or response.ok):
            return True, message or "Success", data
        return False, message or "ไม่พบ contact ใน Zortout", data if isinstance(data, dict) else {}

    def _contact_search_keywords(self, phone, email, contact_code=None):
        keywords = set()
        for value in ((phone or "").strip(), (email or "").strip(), (contact_code or "").strip()):
            if len(value) >= 3:
                keywords.add(value)

        normalized_phone = self._normalize_zortout_phone(phone)
        if len(normalized_phone) >= 3:
            keywords.add(normalized_phone)

        digits = "".join(char for char in (phone or "") if char.isdigit())
        if len(digits) >= 3:
            keywords.add(digits)
            if digits.startswith("66") and len(digits) > 2:
                keywords.add(f"0{digits[2:]}")
        return keywords

    def _find_zortout_contact(self, phone, email, contact_code=None):
        self.ensure_one()
        seen_ids = set()
        contacts = []

        for keyword in self._contact_search_keywords(phone, email, contact_code):
            ok, message, found = self._zortout_get_contacts(keyword)
            if not ok:
                _logger.warning(
                    "Zortout GetContacts search failed for partner %s keyword %s: %s",
                    self.id,
                    keyword,
                    message,
                )
                continue
            for contact in found:
                contact_id = contact.get("id")
                if not contact_id or contact_id in seen_ids:
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

    def _build_zortout_contact_payload(self, user, contact_code=None):
        self.ensure_one()
        return self._compact_zortout_payload({
            "code": contact_code or f"BOPP-{user.id}",
            "name": user.display_name,
            "phone": self._normalize_zortout_phone(user.phone) or (user.phone or "").strip(),
            "email": (user.email or "").strip(),
            "address": self._format_zortout_address(user.address),
            "line": user.line_user_id,
        })

    def _format_zortout_api_error(self, message, data, response=None):
        res_code = self._extract_zortout_res_code(data if isinstance(data, dict) else {})
        res_desc = self._extract_zortout_res_desc(data if isinstance(data, dict) else {})
        parts = [part for part in (message, res_desc) if part]
        if res_code and res_code not in {"200", "201"}:
            parts.append(f"(resCode: {res_code})")
        if response is not None and not parts:
            parts.append(f"HTTP {response.status_code}")
        return " ".join(dict.fromkeys(parts)) or "Zortout API request failed"

    def _zortout_add_contact(self, user, contact_code=None):
        self.ensure_one()
        payload = self._build_zortout_contact_payload(user, contact_code=contact_code)
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
        if ok:
            contact_id = self._extract_zortout_contact_id(data)
            if contact_id:
                return int(contact_id)

        res_code = self._extract_zortout_res_code(data if isinstance(data, dict) else {})
        if res_code == "900":
            existing_contact = self._find_zortout_contact(
                user.phone,
                user.email,
                contact_code or f"BOPP-{user.id}",
            )
            if existing_contact.get("id"):
                existing_code = (existing_contact.get("code") or "").strip() or contact_code
                return self._zortout_update_contact(
                    existing_contact["id"],
                    user,
                    contact_code=existing_code,
                )

        _logger.warning(
            "Zortout AddContact rejected for partner %s user %s (payload=%s): %s",
            self.id,
            user.id,
            payload,
            data,
        )
        raise ValidationError(
            self._format_zortout_api_error(
                message or "ไม่สามารถเพิ่ม contact ใน Zortout ได้",
                data,
                response=response,
            )
        )

    def _zortout_update_contact(self, contact_id, user, contact_code=None):
        self.ensure_one()
        if not contact_id or int(contact_id) <= 0:
            raise ValidationError("ไม่พบ contact id ใน Zortout")

        payload = self._build_zortout_contact_payload(user, contact_code=contact_code)
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
                "Zortout UpdateContact rejected for partner %s contact %s (payload=%s): %s",
                self.id,
                contact_id,
                payload,
                data,
            )
            raise ValidationError(
                self._format_zortout_api_error(
                    message or "ไม่สามารถอัปเดต contact ใน Zortout ได้",
                    data,
                    response=response,
                )
            )
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

    def _mark_zortout_sync_state(self, user, vals):
        user.with_context(skip_zortout_auto_sync=True).write(vals)

    def _resolve_zortout_contact(self, user, phone, email, contact_code):
        self.ensure_one()

        if user.zortout_contact_id and user.zortout_contact_id > 0:
            ok, _message, detail = self._zortout_get_contact_detail(user.zortout_contact_id)
            if ok:
                return detail

        existing_contact = self._find_zortout_contact(phone, email, contact_code)
        if existing_contact.get("id"):
            return existing_contact
        return {}

    def sync_member_to_zortout(self, user):
        self.ensure_one()
        if user.partner_id != self:
            raise ValidationError("สมาชิกไม่ได้อยู่ใน partner นี้")

        self._ensure_zortout_member_sync_ready()

        if not (user.display_name or "").strip():
            raise ValidationError("สมาชิกไม่มีชื่อสำหรับ sync ไป Zortout")

        phone = user.phone
        email = user.email
        contact_code = f"BOPP-{user.id}"

        self._mark_zortout_sync_state(user, {
            "zortout_sync_status": "pending",
            "zortout_sync_error": False,
        })

        existing_contact = self._resolve_zortout_contact(user, phone, email, contact_code)
        existing_contact_id = existing_contact.get("id") if existing_contact else False
        existing_code = (existing_contact.get("code") or "").strip() if existing_contact else ""

        try:
            if existing_contact_id:
                contact_id = self._zortout_update_contact(
                    existing_contact_id,
                    user,
                    contact_code=existing_code or contact_code,
                )
                action = "updated"
            else:
                contact_id = self._zortout_add_contact(user, contact_code=contact_code)
                action = "created"
        except ValidationError as error:
            self._mark_zortout_sync_state(user, {
                "zortout_sync_status": "failed",
                "zortout_sync_error": str(error),
                "zortout_synced_at": fields.Datetime.now(),
            })
            raise

        self._mark_zortout_sync_state(user, {
            "zortout_contact_id": int(contact_id),
            "zortout_sync_status": "synced",
            "zortout_sync_error": False,
            "zortout_synced_at": fields.Datetime.now(),
        })
        return {
            "action": action,
            "contact_id": int(contact_id),
        }
