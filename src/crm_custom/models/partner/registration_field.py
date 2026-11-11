import json
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

SYSTEM_FIELD_KEYS = ("gender", "birth_date", "address")
SYSTEM_FIELD_TYPES = {
    "gender": "gender",
    "birth_date": "date",
    "address": "address",
}
CUSTOM_FIELD_TYPES = ("text", "textarea", "select", "date")
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

DEFAULT_REGISTRATION_FIELDS = (
    {
        "key": "gender",
        "label": "เพศ",
        "field_type": "gender",
        "is_system": True,
        "visible": True,
        "required": True,
        "sequence": 10,
        "options": [],
    },
    {
        "key": "birth_date",
        "label": "วันเกิด",
        "field_type": "date",
        "is_system": True,
        "visible": True,
        "required": True,
        "sequence": 20,
        "options": [],
    },
    {
        "key": "address",
        "label": "ที่อยู่",
        "field_type": "address",
        "is_system": True,
        "visible": False,
        "required": False,
        "sequence": 30,
        "options": [],
    },
)


class PartnerRegistrationField(models.Model):
    _name = "partner.registration.field"
    _description = "Partner Registration Form Field"
    _order = "sequence asc, id asc"
    _sql_constraints = [
        (
            "partner_registration_field_key_uniq",
            "unique(partner_id, key)",
            "Field key must be unique per partner.",
        ),
    ]

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    key = fields.Char(string="Key", required=True)
    label = fields.Char(string="Label", required=True)
    field_type = fields.Selection(
        [
            ("gender", "Gender"),
            ("date", "Date"),
            ("address", "Address"),
            ("text", "Text"),
            ("textarea", "Long Text"),
            ("select", "Select"),
        ],
        string="Type",
        required=True,
        default="text",
    )
    is_system = fields.Boolean(string="System Field", default=False)
    visible = fields.Boolean(string="Visible", default=True)
    required = fields.Boolean(string="Required", default=False)
    sequence = fields.Integer(string="Sequence", default=100)
    active = fields.Boolean(string="Active", default=True)
    options_json = fields.Text(string="Options JSON", default="[]")

    @api.constrains(
        "key",
        "field_type",
        "is_system",
        "visible",
        "required",
        "options_json",
    )
    def _check_registration_field(self):
        for record in self:
            key = (record.key or "").strip()
            if not FIELD_KEY_PATTERN.match(key):
                raise ValidationError(
                    "Field key must be lowercase letters, numbers, or underscore "
                    "(2-64 chars, start with a letter)."
                )
            if record.required and not record.visible:
                raise ValidationError("Required fields must be visible.")

            if record.is_system:
                if key not in SYSTEM_FIELD_KEYS:
                    raise ValidationError(
                        "System fields must use keys: gender, birth_date, address."
                    )
                expected_type = SYSTEM_FIELD_TYPES[key]
                if record.field_type != expected_type:
                    raise ValidationError(
                        f"System field '{key}' must use type '{expected_type}'."
                    )
            else:
                if key in SYSTEM_FIELD_KEYS:
                    raise ValidationError(
                        "Custom fields cannot use system keys: gender, birth_date, address."
                    )
                if record.field_type not in CUSTOM_FIELD_TYPES:
                    raise ValidationError(
                        "Custom field type must be text, textarea, select, or date."
                    )

            options = record._parse_options()
            if record.field_type == "select" and record.active and record.visible:
                if not options:
                    raise ValidationError("Select fields require at least one option.")

    def _parse_options(self):
        self.ensure_one()
        raw = self.options_json or "[]"
        try:
            options = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValidationError("Options JSON is invalid.") from error
        if not isinstance(options, list):
            raise ValidationError("Options must be a JSON array.")

        normalized = []
        for option in options:
            if isinstance(option, dict):
                value = str(option.get("value") or option.get("id") or "").strip()
                label = str(option.get("label") or option.get("name") or value).strip()
            else:
                value = str(option).strip()
                label = value
            if not value:
                continue
            normalized.append({"value": value, "label": label or value})
        return normalized

    def serialize_for_api(self):
        self.ensure_one()
        return {
            "id": self.id,
            "key": self.key,
            "label": self.label,
            "field_type": self.field_type,
            "is_system": bool(self.is_system),
            "visible": bool(self.visible),
            "required": bool(self.required),
            "sequence": self.sequence,
            "active": bool(self.active),
            "options": self._parse_options(),
        }

    @api.model
    def default_schema(self):
        return [dict(item) for item in DEFAULT_REGISTRATION_FIELDS]

    @api.model
    def get_registration_form_for_partner(self, partner, include_hidden=True):
        """Return registration form payload for a partner.

        Partners with no DB rows are ``configured=false`` and receive the
        virtual default schema (address hidden) without writing to the DB.
        """
        fields_records = self.sudo().with_context(active_test=False).search(
            [("partner_id", "=", partner.id)],
            order="sequence asc, id asc",
        )
        if not fields_records:
            return {
                "configured": False,
                "fields": self.default_schema(),
            }

        active_fields = fields_records.filtered(lambda field: field.active)
        serialized = [field.serialize_for_api() for field in active_fields]
        if not include_hidden:
            serialized = [field for field in serialized if field.get("visible")]
        return {
            "configured": True,
            "fields": serialized,
        }

    @api.model
    def replace_partner_fields(self, partner, fields_payload):
        if not isinstance(fields_payload, list):
            raise ValidationError("fields must be an array.")

        existing = self.sudo().with_context(active_test=False).search(
            [("partner_id", "=", partner.id)]
        )
        existing_by_key = {field.key: field for field in existing}
        seen_keys = set()
        sequence = 10

        for index, item in enumerate(fields_payload):
            if not isinstance(item, dict):
                raise ValidationError(f"fields[{index}] must be an object.")

            key = str(item.get("key") or "").strip()
            if not key:
                raise ValidationError(f"fields[{index}].key is required.")
            if key in seen_keys:
                raise ValidationError(f"Duplicate field key: {key}")
            seen_keys.add(key)

            label = str(item.get("label") or "").strip()
            if not label:
                raise ValidationError(f"fields[{index}].label is required.")

            is_system = key in SYSTEM_FIELD_KEYS
            field_type = item.get("field_type") or (
                SYSTEM_FIELD_TYPES[key] if is_system else "text"
            )
            if is_system:
                field_type = SYSTEM_FIELD_TYPES[key]

            visible = bool(item.get("visible", True))
            required = bool(item.get("required", False))
            if required and not visible:
                raise ValidationError(
                    f"Field '{key}' cannot be required while hidden."
                )

            options = item.get("options") or []
            if not isinstance(options, list):
                raise ValidationError(f"fields[{index}].options must be an array.")
            options_json = json.dumps(
                [
                    {
                        "value": str(opt.get("value") if isinstance(opt, dict) else opt).strip(),
                        "label": str(
                            (opt.get("label") if isinstance(opt, dict) else opt)
                            or (opt.get("value") if isinstance(opt, dict) else opt)
                        ).strip(),
                    }
                    for opt in options
                    if str(opt.get("value") if isinstance(opt, dict) else opt).strip()
                ],
                ensure_ascii=False,
            )

            item_sequence = item.get("sequence")
            if item_sequence is None:
                item_sequence = sequence
            sequence = int(item_sequence) + 10

            vals = {
                "partner_id": partner.id,
                "key": key,
                "label": label,
                "field_type": field_type,
                "is_system": is_system,
                "visible": visible,
                "required": required,
                "sequence": int(item_sequence),
                "active": True,
                "options_json": options_json,
            }

            current = existing_by_key.get(key)
            if current:
                if current.is_system and not is_system:
                    raise ValidationError(f"Cannot change system field '{key}'.")
                if current.is_system and (
                    vals["field_type"] != current.field_type
                    or vals["key"] != current.key
                ):
                    raise ValidationError(
                        f"System field '{key}' key/type cannot be changed."
                    )
                current.sudo().write(vals)
            else:
                self.sudo().create(vals)

        # Ensure system fields always exist after first configure.
        for default in DEFAULT_REGISTRATION_FIELDS:
            if default["key"] in seen_keys:
                continue
            current = existing_by_key.get(default["key"])
            vals = {
                "partner_id": partner.id,
                "key": default["key"],
                "label": default["label"],
                "field_type": default["field_type"],
                "is_system": True,
                "visible": False,
                "required": False,
                "sequence": default["sequence"],
                "active": True,
                "options_json": "[]",
            }
            if current:
                current.sudo().write(
                    {
                        "active": True,
                        "visible": False,
                        "required": False,
                        "is_system": True,
                        "field_type": default["field_type"],
                    }
                )
            else:
                self.sudo().create(vals)
            seen_keys.add(default["key"])

        # Soft-remove custom fields that disappeared from the payload.
        for key, field in existing_by_key.items():
            if key in seen_keys:
                continue
            if field.is_system:
                field.sudo().write(
                    {
                        "active": True,
                        "visible": False,
                        "required": False,
                    }
                )
            else:
                field.sudo().write({"active": False, "required": False})

        return self.get_registration_form_for_partner(partner, include_hidden=True)
