import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

ZORTOUT_AUTO_SYNC_FIELDS = frozenset({
    "display_name",
    "phone",
    "email",
    "address",
    "line_user_id",
})


class CrmUserZortoutSync(models.Model):
    _inherit = "crm.user"

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._schedule_zortout_auto_sync()
        return users

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_zortout_auto_sync"):
            return res
        if set(vals) & ZORTOUT_AUTO_SYNC_FIELDS:
            self._schedule_zortout_auto_sync()
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
