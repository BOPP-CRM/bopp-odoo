import json
from datetime import datetime

from odoo import fields, http
from odoo.http import request

from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response
from .get_user_info import GetOrCreateUserController

SYSTEM_KEYS = {"gender", "birth_date", "address"}


class UpdateUserController(http.Controller):

    @http.route("/api/partner/<string:slug>/user", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_user_info(self, slug, **kwargs):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        partner = request.env["partner"].sudo().search(
            [
                ("slug", "=", slug),
            ],
            limit=1,
        )

        if not partner:
            return json_response(
                {
                    "error": "partner_not_found",
                    "message": "ไม่พบ Client ดังกล่าวโปรดติดต่อเจ้าหน้าที่",
                },
                status=404,
            )

        user = request.env["crm.user"].search(
            [
                ("line_user_id", "=", line_profile["userId"]),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if not user:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                status=404,
            )

        form = request.env["partner.registration.field"].get_registration_form_for_partner(
            partner,
            include_hidden=True,
        )
        configured = bool(form.get("configured"))
        field_by_key = {
            field["key"]: field
            for field in form.get("fields") or []
            if field.get("active", True)
        }

        update_vals = {}

        display_name = payload.get("display_name")
        if display_name:
            update_vals["display_name"] = display_name
        elif line_profile.get("displayName"):
            update_vals["display_name"] = line_profile["displayName"]

        validation_error = self._apply_system_fields(
            user=user,
            payload=payload,
            update_vals=update_vals,
            configured=configured,
            field_by_key=field_by_key,
        )
        if validation_error:
            return validation_error

        custom_error = self._apply_custom_fields(
            user=user,
            payload=payload,
            configured=configured,
            field_by_key=field_by_key,
        )
        if custom_error:
            return custom_error

        update_vals["is_updated_user_info"] = True

        changed = user.sudo()._changed_vals(update_vals)
        if changed:
            user.sudo().write(changed)
            user = request.env["crm.user"].sudo().browse(user.id)

        serializer = GetOrCreateUserController()
        return json_response(serializer._serialize_user_response(user, partner))

    def _field_visible(self, configured, field_by_key, key):
        if not configured:
            # Legacy: gender/birth_date always on form; address optional and
            # visibility is handled by CRM client based on is_updated_user_info.
            return key in ("gender", "birth_date", "address")
        field = field_by_key.get(key)
        return bool(field and field.get("visible"))

    def _field_required(self, configured, field_by_key, key):
        if not configured:
            return key in ("gender", "birth_date")
        field = field_by_key.get(key)
        return bool(field and field.get("visible") and field.get("required"))

    def _apply_system_fields(self, user, payload, update_vals, configured, field_by_key):
        if "birth_date" in payload and self._field_visible(
            configured, field_by_key, "birth_date"
        ):
            birth_date = payload.get("birth_date")
            current_birth_date = (
                fields.Date.to_string(user.birth_date) if user.birth_date else ""
            )
            incoming_birth_date = birth_date or ""

            if incoming_birth_date != current_birth_date:
                if current_birth_date:
                    return json_response(
                        {
                            "error": "birth_date_locked",
                            "message": "วันเกิดถูกบันทึกแล้ว ไม่สามารถแก้ไขได้ กรุณาติดต่อเจ้าหน้าที่",
                        },
                        status=400,
                    )
                try:
                    datetime.strptime(birth_date, "%Y-%m-%d")
                except (TypeError, ValueError):
                    return json_response(
                        {
                            "error": "invalid_birth_date",
                            "message": "birth_date must be in yyyy-mm-dd format.",
                        },
                        status=400,
                    )
                update_vals["birth_date"] = birth_date
        elif self._field_required(configured, field_by_key, "birth_date"):
            if not user.birth_date:
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุวันเกิด",
                    },
                    status=400,
                )

        if "gender" in payload and self._field_visible(
            configured, field_by_key, "gender"
        ):
            gender = payload.get("gender") or False
            if gender and gender not in ("M", "F", "O"):
                return json_response(
                    {
                        "error": "invalid_gender",
                        "message": "gender must be M, F, or O.",
                    },
                    status=400,
                )
            update_vals["gender"] = gender
        elif self._field_required(configured, field_by_key, "gender"):
            if not user.gender:
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุเพศ",
                    },
                    status=400,
                )

        if "address" in payload and self._field_visible(
            configured, field_by_key, "address"
        ):
            update_vals["address"] = payload.get("address") or False
        elif self._field_required(configured, field_by_key, "address"):
            if not (user.address or "").strip():
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุที่อยู่",
                    },
                    status=400,
                )

        # Final required checks using values that will be written.
        if self._field_required(configured, field_by_key, "gender"):
            next_gender = update_vals.get("gender", user.gender)
            if not next_gender:
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุเพศ",
                    },
                    status=400,
                )
        if self._field_required(configured, field_by_key, "birth_date"):
            next_birth = update_vals.get(
                "birth_date",
                fields.Date.to_string(user.birth_date) if user.birth_date else False,
            )
            if not next_birth:
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุวันเกิด",
                    },
                    status=400,
                )
        if self._field_required(configured, field_by_key, "address"):
            next_address = update_vals.get("address", user.address)
            if not (next_address or "").strip():
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": "กรุณาระบุที่อยู่",
                    },
                    status=400,
                )

        return None

    def _apply_custom_fields(self, user, payload, configured, field_by_key):
        custom_payload = payload.get("custom_fields")
        if custom_payload is None:
            custom_payload = {}
        if not isinstance(custom_payload, dict):
            return json_response(
                {
                    "error": "invalid_custom_fields",
                    "message": "custom_fields must be an object.",
                },
                status=400,
            )

        if not configured:
            # Legacy partners have no custom registration fields.
            return None

        visible_custom = {
            key: field
            for key, field in field_by_key.items()
            if key not in SYSTEM_KEYS and field.get("visible")
        }

        updates = {}
        for key, value in custom_payload.items():
            key = str(key).strip()
            if key not in visible_custom:
                # Ignore hidden/inactive/unknown keys — never clear stored values.
                continue
            field = visible_custom[key]
            normalized, error = self._normalize_custom_value(field, value)
            if error:
                return error
            updates[key] = normalized

        for key, field in visible_custom.items():
            if not field.get("required"):
                continue
            current_values = user.sudo().get_custom_fields_dict()
            next_value = updates.get(key, current_values.get(key))
            if next_value is None or next_value is False or str(next_value).strip() == "":
                return json_response(
                    {
                        "error": "missing_required_field",
                        "message": f"กรุณาระบุ{field.get('label') or key}",
                    },
                    status=400,
                )

        if updates:
            user.sudo().merge_custom_fields(updates)

        return None

    def _normalize_custom_value(self, field, value):
        field_type = field.get("field_type")
        label = field.get("label") or field.get("key")

        if value is None or value is False:
            return "", None

        if field_type == "select":
            text = str(value).strip()
            allowed = {
                str(option.get("value"))
                for option in (field.get("options") or [])
                if option.get("value") is not None
            }
            if text and allowed and text not in allowed:
                return None, json_response(
                    {
                        "error": "invalid_custom_field",
                        "message": f"ค่าของ{label}ไม่ถูกต้อง",
                    },
                    status=400,
                )
            return text, None

        if field_type == "date":
            text = str(value).strip()
            if not text:
                return "", None
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except (TypeError, ValueError):
                return None, json_response(
                    {
                        "error": "invalid_custom_field",
                        "message": f"{label} must be in yyyy-mm-dd format.",
                    },
                    status=400,
                )
            return text, None

        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False), None

        return str(value), None
