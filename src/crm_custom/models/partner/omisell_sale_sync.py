import json
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

OMISELL_SALE_SYNC_BATCH_SIZE = 5


class PartnerOmisellSaleSyncJob(models.Model):
    _name = "partner.omisell.sale.sync.job"
    _description = "Partner Omisell Sale Sync Job"
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
        ],
        string="State",
        default="pending",
        required=True,
        index=True,
    )
    extra_params = fields.Text(string="Extra Filter Params (JSON)")
    current_page = fields.Integer(string="Current Page", default=1)
    total = fields.Integer(string="Total", default=0)
    processed = fields.Integer(string="Processed", default=0)
    synced = fields.Integer(string="Synced", default=0)
    skipped = fields.Integer(string="Skipped", default=0)
    failed = fields.Integer(string="Failed", default=0)
    last_error = fields.Text(string="Last Error")
    current_order_number = fields.Char(string="Current Order Number")
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
                _logger.exception("Omisell sale sync job %s failed", job.id)
                job.write({
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "current_order_number": False,
                })

    @api.model
    def start_sync_for_partner(self, partner, extra_params=None):
        partner._validate_omisell_configuration()

        active_job = self.search([
            ("partner_id", "=", partner.id),
            ("state", "in", ["pending", "running"]),
        ], limit=1)
        if active_job:
            raise ValidationError("มีงาน sync รายการขาย Omisell กำลังทำงานอยู่แล้ว")

        job = self.create({
            "partner_id": partner.id,
            "state": "pending",
            "current_page": 1,
            "extra_params": json.dumps(extra_params) if extra_params else False,
        })
        job._trigger_processing()
        return job

    def _trigger_processing(self):
        self.ensure_one()
        cron = self.env.ref(
            "crm_custom.ir_cron_omisell_sale_sync",
            raise_if_not_found=False,
        )
        if cron:
            cron._trigger()

        for job in self:
            try:
                job._process_batch()
            except Exception:
                _logger.exception(
                    "Omisell sale sync immediate batch failed for job %s",
                    job.id,
                )

    def _get_extra_params(self):
        self.ensure_one()
        if not self.extra_params:
            return None
        try:
            parsed = json.loads(self.extra_params)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _process_batch(self):
        self.ensure_one()
        if self.state not in {"pending", "running"}:
            return

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
            })

        partner = self.partner_id
        order_model = self.env["partner.omisell.order"].sudo()

        try:
            data = partner.fetch_omisell_order_list(
                page=self.current_page,
                page_size=OMISELL_SALE_SYNC_BATCH_SIZE,
                extra_params=self._get_extra_params(),
            )
        except ValidationError as error:
            self.write({
                "state": "failed",
                "finished_at": fields.Datetime.now(),
                "last_error": str(error),
                "current_order_number": False,
            })
            return

        results = data.get("results") if isinstance(data.get("results"), list) else []
        total_count = data.get("count") or 0
        if not self.total:
            self.write({"total": total_count})

        processed = self.processed
        synced = self.synced
        skipped = self.skipped
        failed = self.failed
        last_error = self.last_error

        for item in results:
            if not isinstance(item, dict):
                continue
            omisell_order_number = (item.get("omisell_order_number") or "").strip()
            if not omisell_order_number:
                continue

            self.write({"current_order_number": omisell_order_number})

            synthetic_payload = {
                "event": "manual_sync",
                "data": {
                    "order_number": item.get("order_number"),
                    "omisell_order_number": omisell_order_number,
                    "status_id": item.get("status_id"),
                    "status_name": item.get("status_name"),
                    "created_time": item.get("created_time"),
                    "updated_time": item.get("updated_time"),
                },
            }
            try:
                result = order_model.process_webhook(partner, synthetic_payload)
                if result.get("status") == "ignored":
                    skipped += 1
                else:
                    synced += 1
            except Exception as error:
                _logger.exception(
                    "Omisell sale sync failed for order %s",
                    omisell_order_number,
                )
                failed += 1
                last_error = str(error)

            processed += 1

        self.write({
            "processed": processed,
            "synced": synced,
            "skipped": skipped,
            "failed": failed,
            "last_error": last_error,
            "current_order_number": False,
        })

        is_last_page = (not results) or (self.current_page * OMISELL_SALE_SYNC_BATCH_SIZE >= total_count)
        if is_last_page:
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
            })
        else:
            self.write({"current_page": self.current_page + 1})

    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "synced": self.synced,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_error": self.last_error or False,
            "started_at": fields.Datetime.to_string(self.started_at)
            if self.started_at
            else False,
            "finished_at": fields.Datetime.to_string(self.finished_at)
            if self.finished_at
            else False,
            "current_order_number": self.current_order_number or False,
            "current_page": self.current_page,
        }

    @api.model
    def get_active_job_for_partner(self, partner):
        job = self.search([
            ("partner_id", "=", partner.id),
            ("state", "in", ["pending", "running"]),
        ], order="create_date desc", limit=1)
        return job.serialize_for_portal() if job else False

    @api.model
    def count_missing_sales_for_partner(self, partner):
        if not (
            partner.omisell_api_key
            and partner.omisell_api_secret
            and partner.omisell_seller_id
        ):
            return 0
        try:
            data = partner.fetch_omisell_order_list(page=1, page_size=1)
        except ValidationError:
            return 0
        return int(data.get("count") or 0)
