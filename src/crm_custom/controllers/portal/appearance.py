import base64
import json
import re

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Public API key -> partner field. Keys mirror the public config serializer
# (``ui_`` prefix stripped) so the portal reads and writes the same shape.
COLOR_FIELDS = {
    "background_color": "ui_background_color",
    "background_white_color": "ui_background_white_color",
    "primary_color": "ui_primary_color",
    "secondary_color": "ui_secondary_color",
    "surface_color": "ui_surface_color",
    "text_color": "ui_text_color",
    "text_white_color": "ui_text_white_color",
    "text_gray_color": "ui_text_gray_color",
    "text_success_color": "ui_text_success_color",
    "text_error_color": "ui_text_error_color",
    "button_color": "ui_button_color",
    "button_text_color": "ui_button_text_color",
}

TEXT_FIELDS = {
    "welcome_title": "ui_welcome_title",
}

BOOL_FIELDS = {
    "crm_required_phone": "ui_crm_required_phone",
    "crm_required_email": "ui_crm_required_email",
}

# API key -> partner char field that stores the image URL.
IMAGE_FIELDS = {
    "logo": "logo",
    "banner": "ui_banner",
}


class PortalAppearanceController(http.Controller):
    @http.route(
        "/api/portal/appearance",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_appearance(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        return json_response(self._serialize_appearance(user.crm_partner_id))

    @http.route(
        "/api/portal/appearance",
        type="http",
        auth="public",
        methods=["PATCH"],
        csrf=False,
        cors="*",
    )
    def update_appearance(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals, validation_error = self._vals_from_payload(payload)
        if validation_error:
            return validation_error

        if vals:
            try:
                partner.sudo().write(vals)
            except ValidationError as error:
                request.env.cr.rollback()
                return json_response(
                    {"error": "invalid_request", "message": error.args[0]},
                    status=400,
                )

        return json_response(self._serialize_appearance(partner))

    @http.route(
        "/api/portal/appearance/image",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def upload_image(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        target, image_data, input_error = self._read_image_input()
        if input_error:
            return input_error

        field_name = IMAGE_FIELDS[target]
        try:
            url = partner._upload_image_field(field_name, image_data)
        except Exception:  # noqa: BLE001 - surface any upload failure as 400
            url = False
        if not url:
            return json_response(
                {"error": "upload_failed", "message": "อัปโหลดรูปภาพไม่สำเร็จ"},
                status=400,
            )

        partner.sudo().write({field_name: url})

        return json_response({
            "url": url,
            "appearance": self._serialize_appearance(partner),
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )
        if not isinstance(payload, dict):
            return None, json_response(
                {"error": "invalid_json", "message": "Body must be a JSON object."},
                status=400,
            )
        return payload, None

    def _vals_from_payload(self, payload):
        vals = {}

        for api_key, field_name in COLOR_FIELDS.items():
            if api_key not in payload:
                continue
            value = payload[api_key]
            if value in (None, ""):
                vals[field_name] = False
                continue
            if not isinstance(value, str) or not HEX_COLOR_PATTERN.match(value):
                return None, json_response(
                    {
                        "error": "invalid_request",
                        "message": f"{api_key} must be a hex color like #1A2B3C.",
                    },
                    status=400,
                )
            vals[field_name] = value

        for api_key, field_name in TEXT_FIELDS.items():
            if api_key in payload:
                value = payload[api_key]
                vals[field_name] = value.strip() if isinstance(value, str) else False

        for api_key, field_name in BOOL_FIELDS.items():
            if api_key in payload:
                vals[field_name] = bool(payload[api_key])

        for api_key, field_name in IMAGE_FIELDS.items():
            for key in (api_key, f"{api_key}_url"):
                if key not in payload:
                    continue
                value = payload[key]
                if not value:
                    vals[field_name] = False
                    break
                value = str(value)
                if not (value.startswith("http://") or value.startswith("https://")):
                    return None, json_response(
                        {
                            "error": "invalid_request",
                            "message": f"{api_key} must be an http(s) URL. "
                            "Use POST /api/portal/appearance/image to upload a file.",
                        },
                        status=400,
                    )
                vals[field_name] = value
                break

        return vals, None

    def _read_image_input(self):
        """Return ``(target, image_base64, error_response)``.

        Accepts either multipart/form-data (``file`` + ``target``) or a JSON body
        (``{"image_base64": ..., "target": "logo"|"banner"}``).
        """
        files = request.httprequest.files
        if files:
            form = request.httprequest.form
            target = (form.get("target") or "").strip().lower()
            upload = files.get("file") or files.get("image")
            if target not in IMAGE_FIELDS:
                return None, None, self._invalid_target_response()
            if not upload:
                return None, None, json_response(
                    {"error": "invalid_request", "message": "กรุณาแนบไฟล์รูปภาพ (field: file)"},
                    status=400,
                )
            return target, base64.b64encode(upload.read()), None

        payload, parse_error = self._parse_payload()
        if parse_error:
            return None, None, parse_error

        target = (payload.get("target") or "").strip().lower()
        if target not in IMAGE_FIELDS:
            return None, None, self._invalid_target_response()

        data = payload.get("image_base64") or payload.get("image")
        if not data or not str(data).strip():
            return None, None, json_response(
                {"error": "invalid_request", "message": "กรุณาส่ง image_base64"},
                status=400,
            )
        return target, data, None

    def _invalid_target_response(self):
        return json_response(
            {
                "error": "invalid_request",
                "message": "target must be one of: %s" % ", ".join(IMAGE_FIELDS),
            },
            status=400,
        )

    def _serialize_appearance(self, partner):
        result = {
            "logo_url": partner.logo or False,
            "banner_url": partner.ui_banner or False,
        }
        for api_key, field_name in TEXT_FIELDS.items():
            result[api_key] = partner[field_name] or False
        for api_key, field_name in BOOL_FIELDS.items():
            result[api_key] = bool(partner[field_name])
        for api_key, field_name in COLOR_FIELDS.items():
            result[api_key] = partner[field_name] or False
        return result
