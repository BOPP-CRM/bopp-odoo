import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .line_messaging import LINE_MULTICAST_MAX_RECIPIENTS, LineRateLimited

_logger = logging.getLogger(__name__)

BROADCAST_RECIPIENT_CHUNK = LINE_MULTICAST_MAX_RECIPIENTS
BROADCAST_MAX_CHUNKS_PER_RUN = 10

_ANY = "any"
_GENDER_SELECTION = [(_ANY, "ทั้งหมด"), ("M", "ชาย"), ("F", "หญิง"), ("O", "อื่นๆ")]
_VERIFIED_SELECTION = [(_ANY, "ทั้งหมด"), ("yes", "ยืนยันแล้ว"), ("no", "ยังไม่ยืนยัน")]


class PartnerBroadcast(models.Model):
    _name = "partner.broadcast"
    _description = "Partner Broadcast"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True, tracking=True)
    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        default="draft",
        required=True,
        index=True,
        tracking=True,
    )
    dry_run = fields.Boolean(string="Dry Run", default=False)
    source = fields.Selection(
        [
            ("manual", "Manual"),
            ("birthday_auto", "Birthday Reminder"),
        ],
        string="Source",
        default="manual",
        required=True,
        index=True,
    )
    birthday_reward_id = fields.Many2one(
        "partner.birthday.reward",
        string="Birthday Reward",
        ondelete="set null",
        index=True,
    )

    message_type = fields.Selection(
        [("text", "Text"), ("flex", "Flex")],
        string="Message Type",
        default="text",
        required=True,
    )
    message_text = fields.Text(string="Message Text")
    flex_payload = fields.Text(string="Flex Payload (JSON)")
    attach_image_url = fields.Char(string="Attach Image URL")
    include_coupon = fields.Boolean(string="Grant Coupon", default=False)
    coupon_id = fields.Many2one(
        "partner.coupon",
        string="Coupon",
        ondelete="restrict",
        domain="[('partner_id', '=', partner_id)]",
    )

    seg_gender = fields.Selection(_GENDER_SELECTION, string="Gender", default=_ANY, required=True)
    seg_age_min = fields.Integer(string="Age Min")
    seg_age_max = fields.Integer(string="Age Max")
    seg_tier_ids = fields.Many2many("partner.tier", string="Tiers")
    seg_signup_from = fields.Datetime(string="Signup From")
    seg_signup_to = fields.Datetime(string="Signup To")
    seg_email_verified = fields.Selection(_VERIFIED_SELECTION, string="Email Verified", default=_ANY, required=True)
    seg_phone_verified = fields.Selection(_VERIFIED_SELECTION, string="Phone Verified", default=_ANY, required=True)

    total_recipients = fields.Integer(string="Total", default=0)
    processed = fields.Integer(string="Processed", default=0)
    coupon_granted = fields.Integer(string="Coupons Granted", default=0)
    line_sent = fields.Integer(string="LINE Sent", default=0)
    line_failed = fields.Integer(string="LINE Failed", default=0)
    preflight_error = fields.Text(string="Preflight Error")
    last_error = fields.Text(string="Last Error")
    started_at = fields.Datetime(string="Started At")
    finished_at = fields.Datetime(string="Finished At")

    pending_recipient_ids = fields.Many2many(
        "crm.user",
        "partner_broadcast_pending_rel",
        "broadcast_id",
        "user_id",
        string="Pending Recipients",
    )
    recipient_ids = fields.One2many(
        "partner.broadcast.recipient",
        "broadcast_id",
        string="Recipients",
    )

    # ------------------------------------------------------------------
    # Audience
    # ------------------------------------------------------------------
    def _build_audience_domain(self):
        self.ensure_one()
        domain = [("partner_id", "=", self.partner_id.id), ("active", "=", True)]

        if self.seg_gender and self.seg_gender != _ANY:
            domain.append(("gender", "=", self.seg_gender))

        if self.seg_tier_ids:
            domain.append(("tier_id", "in", self.seg_tier_ids.ids))

        if self.seg_signup_from:
            domain.append(("create_date", ">=", self.seg_signup_from))
        if self.seg_signup_to:
            domain.append(("create_date", "<=", self.seg_signup_to))

        if self.seg_email_verified == "yes":
            domain.append(("is_email_verified", "=", True))
        elif self.seg_email_verified == "no":
            domain.append(("is_email_verified", "=", False))

        if self.seg_phone_verified == "yes":
            domain.append(("is_phone_verified", "=", True))
        elif self.seg_phone_verified == "no":
            domain.append(("is_phone_verified", "=", False))

        today = fields.Date.context_today(self)
        if self.seg_age_min and self.seg_age_min > 0:
            domain.append(("birth_date", "<=", today - relativedelta(years=self.seg_age_min)))
        if self.seg_age_max and self.seg_age_max > 0:
            domain.append((
                "birth_date",
                ">=",
                today - relativedelta(years=self.seg_age_max + 1) + relativedelta(days=1),
            ))

        return domain

    def _resolve_audience(self):
        self.ensure_one()
        return self.env["crm.user"].sudo().search(self._build_audience_domain(), order="id")

    def preview_audience(self, limit=20):
        self.ensure_one()
        domain = self._build_audience_domain()
        user_model = self.env["crm.user"].sudo()
        count = user_model.search_count(domain)
        sample = user_model.search(domain, limit=limit, order="id")
        return {
            "count": count,
            "sample": [
                {
                    "id": user.id,
                    "display_name": user.display_name,
                    "gender": user.gender or False,
                    "birth_date": fields.Date.to_string(user.birth_date) if user.birth_date else False,
                    "tier": user.tier_id.name if user.tier_id else False,
                }
                for user in sample
            ],
        }

    # ------------------------------------------------------------------
    # Validation / preflight
    # ------------------------------------------------------------------
    def _validate_content(self):
        self.ensure_one()
        # Raises ValidationError when message content is malformed.
        self.env["partner"]._build_line_messages(
            self.message_type,
            self.message_text,
            self.flex_payload,
            self.attach_image_url,
        )

    def _preflight(self, audience_count):
        self.ensure_one()
        self.partner_id._ensure_line_messaging_ready()
        self._validate_content()

        if audience_count <= 0:
            raise ValidationError("ไม่พบสมาชิกที่ตรงกับเงื่อนไขกลุ่มเป้าหมาย")

        if self.include_coupon:
            if not self.coupon_id:
                raise ValidationError("กรุณาเลือกคูปองที่จะแจก")
            if self.coupon_id.partner_id != self.partner_id:
                raise ValidationError("คูปองต้องอยู่ภายใต้ Partner เดียวกัน")
            available = self.coupon_id.available_code_count
            if available < audience_count:
                raise ValidationError(
                    f"โค้ดคูปองในระบบมี {available} ใบ แต่กลุ่มเป้าหมายมี {audience_count} คน "
                    f"กรุณาเพิ่มโค้ดที่หน้าคูปองก่อน"
                )

    # ------------------------------------------------------------------
    # Job lifecycle
    # ------------------------------------------------------------------
    @api.model
    def start_broadcast_for_partner(self, partner, vals):
        vals = dict(vals or {})
        vals["partner_id"] = partner.id
        vals["source"] = "manual"

        active = self.search([
            ("partner_id", "=", partner.id),
            ("source", "=", "manual"),
            ("state", "in", ["pending", "running"]),
        ], limit=1)
        if active:
            raise ValidationError("มี Broadcast กำลังทำงานอยู่แล้ว กรุณารอให้เสร็จก่อน")

        broadcast = self.create(vals)
        users = broadcast._resolve_audience()
        broadcast._launch(users)
        return broadcast

    @api.model
    def _create_broadcast_job(self, partner, users, content_vals, source="manual", birthday_reward_id=False):
        vals = dict(content_vals or {})
        vals.update({
            "partner_id": partner.id,
            "source": source,
            "birthday_reward_id": birthday_reward_id or False,
        })
        broadcast = self.create(vals)
        broadcast._launch(users)
        return broadcast

    def _launch(self, users):
        self.ensure_one()
        try:
            self._preflight(len(users))
        except ValidationError as error:
            self.write({
                "state": "failed",
                "preflight_error": str(error),
                "finished_at": fields.Datetime.now(),
            })
            raise

        recipient_model = self.env["partner.broadcast.recipient"].sudo()
        coupon_status = "pending" if self.include_coupon else "skipped"
        for chunk_start in range(0, len(users), 1000):
            chunk = users[chunk_start:chunk_start + 1000]
            recipient_model.create([
                {
                    "broadcast_id": self.id,
                    "partner_id": self.partner_id.id,
                    "user_id": user.id,
                    "line_user_id": user.line_user_id,
                    "coupon_status": coupon_status,
                    "line_status": "pending",
                }
                for user in chunk
            ])

        self.write({
            "state": "pending",
            "total_recipients": len(users),
            "pending_recipient_ids": [(6, 0, users.ids)],
        })
        self._trigger_processing()

    def _trigger_processing(self):
        self.ensure_one()
        cron = self.env.ref("crm_custom.ir_cron_partner_broadcast", raise_if_not_found=False)
        if cron:
            cron._trigger()
        try:
            self._process_batch(max_chunks=1)
        except Exception:
            _logger.exception("Broadcast %s immediate batch failed", self.id)

    @api.model
    def _cron_process_pending_jobs(self):
        jobs = self.search([
            ("state", "in", ["pending", "running"]),
        ], order="create_date asc", limit=10)
        for job in jobs:
            try:
                job._process_batch()
            except Exception:
                _logger.exception("Broadcast job %s failed", job.id)
                job.write({
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                })

    def _process_batch(self, max_chunks=None):
        self.ensure_one()
        if self.state not in ("pending", "running"):
            return
        if self.state == "pending":
            self.write({"state": "running", "started_at": fields.Datetime.now()})

        max_chunks = max_chunks or BROADCAST_MAX_CHUNKS_PER_RUN
        for _iteration in range(max_chunks):
            users = self.pending_recipient_ids[:BROADCAST_RECIPIENT_CHUNK]
            if not users:
                self.write({"state": "done", "finished_at": fields.Datetime.now()})
                return

            recipients = self.env["partner.broadcast.recipient"].sudo().search([
                ("broadcast_id", "=", self.id),
                ("user_id", "in", users.ids),
            ])
            recipient_by_user = {r.user_id.id: r for r in recipients}

            if self.include_coupon and not self._grant_coupons(users, recipient_by_user):
                return
            if not self._send_line(users, recipient_by_user):
                return

            self.write({
                "pending_recipient_ids": [(3, user.id) for user in users],
                "processed": self.processed + len(users),
            })
            if not self.env.context.get("broadcast_no_commit"):
                self.env.cr.commit()

        if not self.pending_recipient_ids:
            self.write({"state": "done", "finished_at": fields.Datetime.now()})

    def _grant_coupons(self, users, recipient_by_user):
        """Return True to continue, False to stop the job (pool empty)."""
        self.ensure_one()
        note = f"Broadcast: {self.name}"
        year = fields.Date.context_today(self).year
        granted = 0
        for user in users:
            recipient = recipient_by_user.get(user.id)
            if not recipient or recipient.coupon_status != "pending":
                continue

            if self.birthday_reward_id and self._already_has_birthday_coupon(user, year):
                recipient.coupon_status = "skipped"
                continue

            try:
                user_coupon = self.coupon_id.grant_to_user(user, note)
            except ValidationError as error:
                if "คูปองหมด" in str(error):
                    recipient.coupon_status = "pool_empty"
                    self.write({
                        "state": "failed",
                        "last_error": str(error),
                        "coupon_granted": self.coupon_granted + granted,
                        "finished_at": fields.Datetime.now(),
                    })
                    return False
                recipient.write({"coupon_status": "error", "line_error": str(error)})
                continue

            if self.birthday_reward_id:
                user_coupon.birthday_reward_id = self.birthday_reward_id.id
            recipient.write({"coupon_status": "granted", "user_coupon_id": user_coupon.id})
            granted += 1

        if granted:
            self.coupon_granted += granted
        return True

    def _already_has_birthday_coupon(self, user, year):
        self.ensure_one()
        return bool(self.env["crm.user.coupon"].sudo().search_count([
            ("user_id", "=", user.id),
            ("birthday_reward_id", "=", self.birthday_reward_id.id),
            ("acquired_date", ">=", date(year, 1, 1)),
        ]))

    def _send_line(self, users, recipient_by_user):
        """Return True to continue, False to stop this run (rate limited)."""
        self.ensure_one()
        messages = self.env["partner"]._build_line_messages(
            self.message_type,
            self.message_text,
            self.flex_payload,
            self.attach_image_url,
        )
        line_user_ids = [user.line_user_id for user in users if user.line_user_id]

        if self.dry_run:
            for recipient in recipient_by_user.values():
                recipient.line_status = "skipped"
            return True

        try:
            if len(line_user_ids) == 1:
                self.partner_id._line_push(line_user_ids[0], messages)
            else:
                self.partner_id._line_multicast(line_user_ids, messages)
        except LineRateLimited:
            _logger.info("Broadcast %s rate limited, will retry", self.id)
            return False
        except ValidationError as error:
            for recipient in recipient_by_user.values():
                recipient.write({"line_status": "failed", "line_error": str(error)})
            self.write({
                "line_failed": self.line_failed + len(recipient_by_user),
                "last_error": str(error),
            })
            return True

        for recipient in recipient_by_user.values():
            recipient.line_status = "sent"
        self.line_sent += len(line_user_ids)
        return True

    def action_cancel(self):
        for record in self:
            if record.state in ("pending", "running"):
                record.write({"state": "cancelled", "finished_at": fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def serialize_for_portal(self):
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "source": self.source,
            "dry_run": self.dry_run,
            "message_type": self.message_type,
            "include_coupon": self.include_coupon,
            "coupon": {"id": self.coupon_id.id, "name": self.coupon_id.name} if self.coupon_id else False,
            "total_recipients": self.total_recipients,
            "processed": self.processed,
            "coupon_granted": self.coupon_granted,
            "line_sent": self.line_sent,
            "line_failed": self.line_failed,
            "preflight_error": self.preflight_error or False,
            "last_error": self.last_error or False,
            "started_at": fields.Datetime.to_string(self.started_at) if self.started_at else False,
            "finished_at": fields.Datetime.to_string(self.finished_at) if self.finished_at else False,
            "create_date": fields.Datetime.to_string(self.create_date),
        }

    @api.model
    def get_active_broadcast_for_partner(self, partner):
        job = self.search([
            ("partner_id", "=", partner.id),
            ("source", "=", "manual"),
            ("state", "in", ["pending", "running"]),
        ], order="create_date desc", limit=1)
        return job.serialize_for_portal() if job else False

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("coupon_id", "seg_tier_ids", "partner_id")
    def _check_partner_consistency(self):
        for record in self:
            if record.coupon_id and record.coupon_id.partner_id != record.partner_id:
                raise ValidationError("คูปองต้องอยู่ภายใต้ Partner เดียวกัน")
            outside = record.seg_tier_ids.filtered(lambda tier: tier.partner_id != record.partner_id)
            if outside:
                raise ValidationError("Tier ที่เลือกต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.constrains("seg_age_min", "seg_age_max")
    def _check_age_range(self):
        for record in self:
            if record.seg_age_min < 0 or record.seg_age_max < 0:
                raise ValidationError("อายุต้องไม่ติดลบ")
            if record.seg_age_min and record.seg_age_max and record.seg_age_min > record.seg_age_max:
                raise ValidationError("อายุขั้นต่ำต้องไม่มากกว่าอายุสูงสุด")
