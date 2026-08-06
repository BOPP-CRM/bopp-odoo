import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

RECEIPT_SALE_SYNC_BATCH_SIZE = 2


class PartnerReceiptSaleSyncJob(models.Model):
    _name = "partner.receipt.sale.sync.job"
    _description = "Partner Receipt Sale Sync Job"
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
    pending_receipt_ids = fields.Many2many(
        "crm.partner.receipt.redeem",
        "partner_receipt_sale_sync_job_pending_rel",
        "job_id",
        "receipt_id",
        string="Pending Receipts",
    )
    current_receipt_id = fields.Many2one(
        "crm.partner.receipt.redeem",
        string="Current Receipt",
        ondelete="set null",
    )
    total = fields.Integer(string="Total", default=0)
    processed = fields.Integer(string="Processed", default=0)
    synced = fields.Integer(string="Synced", default=0)
    skipped = fields.Integer(string="Skipped", default=0)
    failed = fields.Integer(string="Failed", default=0)
    last_error = fields.Text(string="Last Error")
    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")

    @api.model
    def _cron_process_pending_jobs(self):
        jobs = self.search([
            ("state", "in", ["pending", "running"]),
        ], order="create_date asc", limit=5)
        for job in jobs:
            try:
                job._process_batch()
            except Exception:
                _logger.exception("Receipt sale sync job %s failed", job.id)
                job.write({
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "current_receipt_id": False,
                })

    @api.model
    def start_sync_for_partner(self, partner):
        partner._ensure_openai_api_key()

        active_job = self.search([
            ("partner_id", "=", partner.id),
            ("state", "in", ["pending", "running"]),
        ], limit=1)
        if active_job:
            raise ValidationError("มีงาน sync ใบเสร็จไปรายการขายกำลังทำงานอยู่แล้ว")

        sale_model = self.env["partner.sale"].sudo()
        missing_receipts = sale_model.find_receipts_missing_sale(partner)
        if not missing_receipts:
            raise ValidationError("ไม่พบใบเสร็จที่อนุมัติแล้วที่ยังไม่ได้ sync รายการขาย")

        job = self.create({
            "partner_id": partner.id,
            "state": "pending",
            "total": len(missing_receipts),
            "pending_receipt_ids": [(6, 0, missing_receipts.ids)],
        })
        job._trigger_processing()
        return job

    def _trigger_processing(self):
        self.ensure_one()
        cron = self.env.ref(
            "crm_custom.ir_cron_receipt_sale_sync",
            raise_if_not_found=False,
        )
        if cron:
            cron._trigger()

        for job in self:
            try:
                job._process_batch()
            except Exception:
                _logger.exception(
                    "Receipt sale sync immediate batch failed for job %s",
                    job.id,
                )

    def _process_batch(self):
        self.ensure_one()
        if self.state not in {"pending", "running"}:
            return

        if self.state == "pending":
            self.write({
                "state": "running",
                "started_at": fields.Datetime.now(),
            })

        receipts = self.pending_receipt_ids[:RECEIPT_SALE_SYNC_BATCH_SIZE]
        if not receipts:
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
                "current_receipt_id": False,
            })
            return

        partner = self.partner_id
        sale_model = self.env["partner.sale"].sudo()
        processed = self.processed
        synced = self.synced
        skipped = self.skipped
        failed = self.failed
        last_error = self.last_error

        for receipt in receipts:
            self.write({"current_receipt_id": receipt.id})
            try:
                existing = sale_model.search([
                    ("partner_id", "=", partner.id),
                    ("receipt_redeem_id", "=", receipt.id),
                ], limit=1)
                if existing:
                    skipped += 1
                else:
                    result = sale_model.sync_receipt_with_openai(partner, receipt)
                    if result.get("skipped"):
                        skipped += 1
                    elif result.get("sale_id"):
                        synced += 1
                    else:
                        skipped += 1
            except ValidationError as error:
                failed += 1
                last_error = str(error)
            except Exception as error:
                _logger.exception(
                    "Unexpected receipt sale sync failure for receipt %s",
                    receipt.id,
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
                "pending_receipt_ids": [(3, receipt.id)],
            })

        if not self.pending_receipt_ids:
            self.write({
                "state": "done",
                "finished_at": fields.Datetime.now(),
                "current_receipt_id": False,
            })

    def serialize_for_portal(self):
        self.ensure_one()
        current_receipt = self.current_receipt_id
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
            "current_receipt": {
                "id": current_receipt.id,
                "receipt_number": current_receipt.receipt_number,
            } if current_receipt else False,
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
        return len(
            self.env["partner.sale"].sudo().find_receipts_missing_sale(partner)
        )
