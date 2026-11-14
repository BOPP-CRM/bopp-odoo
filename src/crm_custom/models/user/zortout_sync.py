import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ZORTOUT_AUTO_SYNC_FIELDS = frozenset({
    "display_name",
    "phone",
    "email",
    "address",
    "line_user_id",
})

_COMPARED_FIELD_TYPES = frozenset({"char", "text", "html", "boolean", "selection", "date"})


def _blank_to_false(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value if value != "" else False
    return value


class CrmUserZortoutSync(models.Model):
    _inherit = "crm.user"

    def _field_value_matches(self, field_name, new_value):
        """Whether ``new_value`` would leave the stored field unchanged.

        Unknown field types report a mismatch so a mixed write is not dropped.
        """
        self.ensure_one()
        field = self._fields.get(field_name)
        if field is None or field.type not in _COMPARED_FIELD_TYPES:
            return False

        current = self[field_name]
        if field.type in ("char", "text", "html"):
            return _blank_to_false(current) == _blank_to_false(new_value)
        if field.type == "boolean":
            return bool(current) == bool(new_value)
        if field.type == "selection":
            incoming = False if new_value in (None, False, "") else new_value
            return (current or False) == incoming
        if field.type == "date":
            current_value = fields.Date.to_string(current) if current else False
            if new_value in (None, False, ""):
                return current_value is False
            try:
                incoming = fields.Date.to_string(fields.Date.to_date(new_value))
            except (TypeError, ValueError):
                return False
            return current_value == incoming
        return False

    def _changed_vals(self, vals):
        """Subset of ``vals`` whose values differ from this record."""
        self.ensure_one()
        return {
            name: value
            for name, value in vals.items()
            if not self._field_value_matches(name, value)
        }

    def _users_needing_zortout_auto_sync(self, vals):
        """Records whose sync-relevant fields would actually change."""
        relevant = [name for name in vals if name in ZORTOUT_AUTO_SYNC_FIELDS]
        if not relevant:
            return self.browse()
        changed = self.browse()
        for user in self:
            for name in relevant:
                if not user._field_value_matches(name, vals[name]):
                    changed |= user
                    break
        return changed

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._schedule_zortout_auto_sync()
        return users

    def write(self, vals):
        if self.env.context.get("skip_zortout_auto_sync"):
            return super().write(vals)
        to_sync = self._users_needing_zortout_auto_sync(vals)
        res = super().write(vals)
        if to_sync:
            to_sync._schedule_zortout_auto_sync()
        return res

    def _schedule_zortout_auto_sync(self):
        users = self.filtered(
            lambda user: user.active and user.partner_id._is_zortout_member_sync_enabled()
        )
        if not users:
            return

        user_ids = users.ids
        dbname = self.env.cr.dbname

        @self.env.cr.postcommit.add
        def _sync_after_commit():
            from odoo.modules.registry import Registry

            try:
                with Registry(dbname).cursor() as cr:
                    env = api.Environment(cr, api.SUPERUSER_ID, {})
                    for user in env["crm.user"].browse(user_ids).exists():
                        partner = user.partner_id
                        try:
                            partner.sync_member_to_zortout(user)
                        except Exception as error:
                            _logger.info(
                                "Zortout auto sync failed for user %s: %s",
                                user.id,
                                error,
                            )
                    cr.commit()
            except Exception:
                _logger.exception("Zortout auto sync postcommit failed")
