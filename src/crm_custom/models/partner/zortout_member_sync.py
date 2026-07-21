import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

ZORTOUT_MEMBER_SYNC_BATCH_SIZE = 5


class PartnerZortoutMemberSyncJob(models.Model):
    _name = "partner.zortout.member.sync.job"
    _description = "Partner Zortout Member Sync Job"
    _order = "create_date desc"

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        default="pending",
        required=True,
        index=True,
    )
    pending_user_ids = fields.Many2many(
        "crm.user",
        "partner_zortout_member_sync_job_pending_rel",
        "job_id",
        "user_id",
        string="Pending Users",
    )
    current_user_id = fields.Many2one("crm.user", string="Current User", ondelete="set null")
    total = fields.Integer(string="Total", default=0)
    processed = fields.Integer(string="Processed", default=0)
    succeeded = fields.Integer(string="Succeeded", default=0)
    failed = fields.Integer(string="Failed", default=0)
    last_error = fields.Text(string="Last Error")
    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")

    @api.model
    def _cron_process_pending_jobs(self):
        jobs = self.search([
            ("state", "in", ["pending", "running"]),
        ], order="create_date asc", limit=10)
        for job in jobs:
            try:
                job._process_batch()
            except Exception:
                _logger.exception("Zortout member sync job %s failed", job.id)
                job.write({
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "current_user_id": False,
                })

    @api.model
    def start_sync_for_partner(self, partner, user_ids=None):
        partner._ensure_zortout_member_sync_ready()

        active_job = self.search([
            ("partner_id", "=", partner.id),
            ("state", "in", ["pending", "running"]),
        ], limit=1)
        if active_job:
            raise ValidationError("มีงาน sync สมาชิก Zortout กำลังทำงานอยู่แล้ว")

        user_model = self.env["crm.user"].sudo()
        domain = [("partner_id", "=", partner.id), ("active", "=", True)]
        if user_ids:
            domain.append(("id", "in", user_ids))

        users = user_model.search(domain, order="id asc")
        if not users:
            raise ValidationError("ไม่พบสมาชิกที่จะ sync")

        job = self.create({
            "partner_id": partner.id,
            "state": "pending",
            "total": len(users),
            "pending_user_ids": [(6, 0, users.ids)],
        })
        job._trigger_processing()
        return job

    def _trigger_processing(self):
        self.ensure_one()
        cron = self.env.ref(
            "crm_custom.ir_cron_zortout_member_sync",
            raise_if_not_found=False,
        )
        if cron:
            cron._trigger()

        for job in self:
            try:
                job._process_batch()
            except Exception:
                _logger.exception("Zortout member sync immediate batch failed for job %s", job.id)

    def _process_batch(self):
        self.ensure_one()
        if self.state not in {"pending", "running"}:
            return

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
            })

        users = self.pending_user_ids[:ZORTOUT_MEMBER_SYNC_BATCH_SIZE]
        if not users:
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
                "current_user_id": False,
            })
            return

        partner = self.partner_id
        processed = self.processed
        succeeded = self.succeeded
        failed = self.failed
        last_error = self.last_error

        for user in users:
            self.write({"current_user_id": user.id})
            try:
                partner.sync_member_to_zortout(user)
                succeeded += 1
            except ValidationError as error:
                failed += 1
                last_error = str(error)
            except Exception as error:
                _logger.exception(
                    "Unexpected Zortout member sync failure for user %s",
                    user.id,
                )
                failed += 1
                last_error = str(error)

            processed += 1
            self.write({
                "processed": processed,
                "succeeded": succeeded,
                "failed": failed,
                "last_error": last_error,
                "pending_user_ids": [(3, user.id)],
            })

        if not self.pending_user_ids:
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
                "current_user_id": False,
            })

    def serialize_for_portal(self):
        self.ensure_one()
        current_user = self.current_user_id
        return {
            "id": self.id,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "last_error": self.last_error or False,
            "started_at": fields.Datetime.to_string(self.started_at) if self.started_at else False,
            "finished_at": fields.Datetime.to_string(self.finished_at) if self.finished_at else False,
            "current_user": {
                "id": current_user.id,
                "display_name": current_user.display_name,
            } if current_user else False,
        }

    @api.model
    def get_active_job_for_partner(self, partner):
        job = self.search([
            ("partner_id", "=", partner.id),
            ("state", "in", ["pending", "running"]),
        ], order="create_date desc", limit=1)
        return job.serialize_for_portal() if job else False
