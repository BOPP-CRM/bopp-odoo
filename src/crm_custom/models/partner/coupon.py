import base64
import csv
import io
import secrets
import string
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

MAX_CODE_BATCH_SIZE = 2000


class PartnerCoupon(models.Model):
    _name = "partner.coupon"
    _description = "Partner Coupon"
    _inherit = ["s3.image.mixin", "mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(string="Name", required=True, tracking=True)
    image = fields.Char(string="Image", tracking=True)
    image_file = fields.Image(
        string="Image",
        max_width=1000,
        max_height=1000,
        store=False,
        compute="_compute_image_file",
        inverse="_inverse_image_file",
    )
    code_source = fields.Selection(
        [
            ("generate", "Generate"),
            ("import", "Import CSV"),
        ],
        string="Code Source",
        required=True,
        default="generate",
        tracking=True,
    )
    prefix_code = fields.Char(string="Prefix Code", tracking=True)
    random_range = fields.Integer(string="Random Range", default=6, tracking=True)
    suffix_code = fields.Char(string="Suffix Code", tracking=True)
    locked_random_range = fields.Integer(
        string="Locked Random Range",
        readonly=True,
        help="Minimum random range for future code generation.",
    )
    value = fields.Float(string="Value", required=True, default=0, tracking=True)
    start_time = fields.Datetime(string="Start Date", default=fields.Datetime.now, required=True, tracking=True)
    term_and_condition = fields.Text(string="Term and Condition", tracking=True)
    code_expiry_interval = fields.Integer(
        string="Expiry Interval (Minutes)",
        default=15,
        tracking=True,
    )
    end_time = fields.Datetime(string="End Date", tracking=True)
    is_show_in_ui = fields.Boolean(
        string="Show In UI",
        default=True,
        tracking=True,
        help="แสดงคูปองนี้ในหน้าบ้านสำหรับให้ผู้ใช้แลก",
    )
    max_redeem_per_user = fields.Integer(
        string="Max Redeem Per User",
        default=0,
        tracking=True,
        help="จำกัดจำนวนครั้งที่ผู้ใช้แต่ละคนแลกได้ (0 = ไม่จำกัด)",
    )
    total_code_count = fields.Integer(
        string="Total Codes",
        compute="_compute_code_counts",
    )
    available_code_count = fields.Integer(
        string="Available Codes",
        compute="_compute_code_counts",
    )
    redeemed_count = fields.Integer(
        string="Redeemed Count",
        compute="_compute_code_counts",
    )
    used_code_count = fields.Integer(
        string="Used Codes",
        compute="_compute_code_counts",
    )

    currency_id = fields.Many2one(
        "crm.partner.currency",
        string="Currency",
        required=True,
        ondelete="restrict",
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    coupon_code_ids = fields.One2many(
        "partner.coupon.code",
        "coupon_id",
        string="Coupon Codes",
    )

    user_coupon_ids = fields.One2many(
        "crm.user.coupon",
        "coupon_id",
        string="User Coupons",
    )

    def _get_s3_image_config(self):
        return {
            "image": {
                "max_width": 1000,
                "max_height": 1000,
            },
        }

    @api.depends("image")
    def _compute_image_file(self):
        self._compute_s3_image_file("image")

    def _inverse_image_file(self):
        self._inverse_s3_image_file("image")

    @api.depends("coupon_code_ids", "coupon_code_ids.state")
    def _compute_code_counts(self):
        for record in self:
            codes = record.coupon_code_ids
            record.total_code_count = len(codes)
            record.available_code_count = len(codes.filtered(lambda code: code.state == "available"))
            record.redeemed_count = len(codes.filtered(lambda code: code.state == "redeemed"))
            record.used_code_count = len(codes.filtered(lambda code: code.state == "used"))

    def redeem_for_user(self, user):
        self.ensure_one()
        self._check_can_redeem(user)

        coupon_code = self.env["partner.coupon.code"].sudo().search(
            [
                ("coupon_id", "=", self.id),
                ("state", "=", "available"),
            ],
            limit=1,
            order="id",
        )
        if not coupon_code:
            raise ValidationError("คูปองหมดแล้ว")

        now = fields.Datetime.now()

        point = self.env["crm.user.point"].sudo().create({
            "name": f"แลกคูปอง {self.name}",
            "value": self.value,
            "type": "burn",
            "given_date": now,
            "currency_id": self.currency_id.id,
            "user_id": user.id,
        })

        user_coupon = self.env["crm.user.coupon"].sudo().create({
            "name": self.name,
            "code": coupon_code.code,
            "value": self.value,
            "acquired_date": now,
            "expiration_date": self.end_time or False,
            "currency_id": self.currency_id.id,
            "partner_id": self.partner_id.id,
            "coupon_id": self.id,
            "user_id": user.id,
            "point_id": point.id,
            "coupon_code_id": coupon_code.id,
        })

        coupon_code.write({
            "state": "redeemed",
            "user_coupon_id": user_coupon.id,
            "redeemed_by_user_id": user.id,
            "redeemed_date": now,
        })

        return user_coupon

    def grant_to_user(self, user, note, point_redeem_id=False):
        self.ensure_one()
        if not note or not note.strip():
            raise ValidationError("กรุณาระบุหมายเหตุ")

        if user.partner_id != self.partner_id:
            raise ValidationError("ผู้ใช้อยู่นอกเหนือ Application นี้")

        coupon_code = self.env["partner.coupon.code"].sudo().search(
            [
                ("coupon_id", "=", self.id),
                ("state", "=", "available"),
            ],
            limit=1,
            order="id",
        )
        if not coupon_code:
            raise ValidationError("คูปองหมดแล้ว")

        now = fields.Datetime.now()

        user_coupon = self.env["crm.user.coupon"].sudo().create({
            "name": self.name,
            "admin_note": note.strip(),
            "code": coupon_code.code,
            "value": self.value,
            "acquired_date": now,
            "expiration_date": self.end_time or False,
            "currency_id": self.currency_id.id,
            "partner_id": self.partner_id.id,
            "coupon_id": self.id,
            "user_id": user.id,
            "coupon_code_id": coupon_code.id,
            "point_redeem_id": point_redeem_id or False,
        })

        coupon_code.write({
            "state": "redeemed",
            "user_coupon_id": user_coupon.id,
            "redeemed_by_user_id": user.id,
            "redeemed_date": now,
        })

        return user_coupon

    def get_codes_export_csv_content(self):
        self.ensure_one()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "code",
            "status",
            "redeemed_by",
            "redeemed_date",
            "used_by",
            "used_date",
        ])

        for coupon_code in self.coupon_code_ids.sorted("code"):
            writer.writerow([
                coupon_code.code,
                coupon_code.state,
                coupon_code.redeemed_by_user_id.line_user_id or "",
                fields.Datetime.to_string(coupon_code.redeemed_date) if coupon_code.redeemed_date else "",
                coupon_code.used_by_user_id.line_user_id or "",
                fields.Datetime.to_string(coupon_code.used_date) if coupon_code.used_date else "",
            ])

        return output.getvalue()

    def get_codes_export_filename(self):
        self.ensure_one()
        safe_name = "".join(
            char if char.isascii() and (char.isalnum() or char in {"-", "_"}) else "_"
            for char in (self.name or "coupon")
        ).strip("_") or f"coupon_{self.id}"
        return f"{safe_name}_codes.csv"

    def action_export_codes(self):
        self.ensure_one()

        attachment = self.env["ir.attachment"].sudo().create({
            "name": self.get_codes_export_filename(),
            "datas": base64.b64encode(self.get_codes_export_csv_content().encode("utf-8")),
            "mimetype": "text/csv",
            "res_model": self._name,
            "res_id": self.id,
        })

        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    @api.model
    def get_import_template_csv_content(self):
        return "code\nEXAMPLE001\nEXAMPLE002\n"

    @api.model
    def action_download_import_template(self):
        csv_content = self.get_import_template_csv_content()
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "coupon_code_import_template.csv",
            "datas": base64.b64encode(csv_content.encode("utf-8")),
            "mimetype": "text/csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def add_codes(self, add_source, code_quantity=1, prefix_code=None, random_range=None,
                  suffix_code=None, import_file=None, import_filename=None):
        self.ensure_one()

        if add_source == "generate":
            self._validate_code_batch_size(code_quantity)
            effective_random_range = random_range or self.random_range
            if effective_random_range is None:
                effective_random_range = max(self.locked_random_range or 0, 6)

            effective_prefix = (
                (prefix_code or self.prefix_code)
                if prefix_code is not None
                else self.prefix_code
            )
            effective_suffix = (
                (suffix_code or self.suffix_code)
                if suffix_code is not None
                else self.suffix_code
            )
            self.write({
                "prefix_code": effective_prefix,
                "suffix_code": effective_suffix,
            })
            self.create_generated_codes(code_quantity, effective_random_range)
            return code_quantity

        if not import_file:
            raise ValidationError("กรุณาอัปโหลดไฟล์ CSV")

        before_count = len(self.coupon_code_ids)
        self.import_codes_from_file(import_file, import_filename)
        return len(self.coupon_code_ids) - before_count

    @api.model
    def _validate_code_batch_size(self, count):
        if count <= 0:
            raise ValidationError("จำนวน code ต้องมากกว่า 0")
        if count > MAX_CODE_BATCH_SIZE:
            raise ValidationError(
                f"สามารถเพิ่ม code ได้สูงสุด {MAX_CODE_BATCH_SIZE} รายการต่อครั้ง"
            )

    def action_add_codes(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Add Coupon Codes",
            "res_model": "partner.coupon.add.codes.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_coupon_id": self.id,
                "default_partner_id": self.partner_id.id,
                "default_random_range": self.random_range,
            },
        }

    def create_generated_codes(self, quantity, random_range=None):
        self.ensure_one()
        self._validate_code_batch_size(quantity)

        effective_random_range = random_range or self.random_range
        locked_random_range = self.locked_random_range or 0
        if effective_random_range < locked_random_range:
            raise ValidationError(
                f"Random range ต้องมากกว่าหรือเท่ากับ {locked_random_range}"
            )

        if effective_random_range <= 0:
            raise ValidationError("Random range ต้องมากกว่า 0")

        codes = self._generate_unique_codes(quantity, effective_random_range)
        self._create_codes(codes)

        self.write({
            "random_range": effective_random_range,
            "locked_random_range": max(locked_random_range, effective_random_range),
        })

    def import_codes_from_file(self, file_data, filename=None):
        self.ensure_one()
        codes = self._parse_codes_from_csv(file_data, filename)
        self._create_codes(codes)

    def _generate_unique_codes(self, quantity, random_range):
        self.ensure_one()
        alphabet = string.ascii_uppercase + string.digits
        prefix = self.prefix_code or ""
        suffix = self.suffix_code or ""
        existing_codes = self._get_existing_codes()
        generated_codes = []

        attempts = 0
        max_attempts = quantity * 100
        while len(generated_codes) < quantity and attempts < max_attempts:
            attempts += 1
            random_part = "".join(
                secrets.choice(alphabet) for _index in range(random_range)
            )
            code = f"{prefix}{random_part}{suffix}"
            if code in existing_codes:
                continue
            existing_codes.add(code)
            generated_codes.append(code)

        if len(generated_codes) < quantity:
            raise ValidationError("ไม่สามารถสร้าง Coupon Code ที่ไม่ซ้ำกันได้")

        return generated_codes

    def _create_codes(self, codes):
        self.ensure_one()
        self._validate_code_batch_size(len(codes))
        seen_codes = set()
        duplicate_codes_in_file = []
        for code in codes:
            if code in seen_codes:
                duplicate_codes_in_file.append(code)
            seen_codes.add(code)
        if duplicate_codes_in_file:
            preview = ", ".join(sorted(set(duplicate_codes_in_file))[:5])
            raise ValidationError(f"พบ code ซ้ำในไฟล์: {preview}")

        existing_codes = self._get_existing_codes()
        duplicate_codes = sorted({code for code in codes if code in existing_codes})
        if duplicate_codes:
            preview = ", ".join(duplicate_codes[:5])
            raise ValidationError(f"พบ code ที่ซ้ำกัน: {preview}")

        self.env["partner.coupon.code"].create([
            {
                "coupon_id": self.id,
                "partner_id": self.partner_id.id,
                "code": code,
            }
            for code in codes
        ])

    def _get_existing_codes(self):
        self.ensure_one()
        existing_codes = set(
            self.env["partner.coupon.code"].sudo().search([]).mapped("code")
        )
        existing_codes.update(
            self.env["crm.user.coupon"].sudo().search([]).mapped("code")
        )
        return existing_codes

    def _parse_codes_from_csv(self, file_data, filename=None):
        if not file_data:
            raise ValidationError("กรุณาอัปโหลดไฟล์ CSV")

        try:
            content = base64.b64decode(file_data)
        except Exception as error:
            raise ValidationError("ไฟล์ CSV ไม่ถูกต้อง") from error

        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")

        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            raise ValidationError("ไฟล์ CSV ว่างเปล่า")

        header = [cell.strip().lower() for cell in rows[0]]
        if "code" in header:
            code_index = header.index("code")
            data_rows = rows[1:]
        else:
            code_index = 0
            data_rows = rows

        codes = []
        for row in data_rows:
            if code_index >= len(row):
                continue
            code = row[code_index].strip()
            if code:
                codes.append(code)

        if not codes:
            raise ValidationError("ไม่พบ code ในไฟล์ CSV")

        return codes

    def _check_can_redeem(self, user):
        self.ensure_one()
        now = fields.Datetime.now()

        if user.partner_id != self.partner_id:
            raise ValidationError("ผู้ใช้อยู่นอกเหนือ Application นี้")

        if self.currency_id.partner_id != self.partner_id:
            raise ValidationError("Coupon Currency อยู่นอกเหนือ Application นี้")

        if self.value < 0:
            raise ValidationError("Value ต้องมากกว่า 0")

        if self.start_time and now < self.start_time:
            raise ValidationError("Coupon ยังไม่ได้เปิดใช้ตอนนี้")

        if self.end_time and now > self.end_time:
            raise ValidationError("Coupon หมดอายุแล้ว")

        if self._get_user_balance(user) < self.value:
            raise ValidationError("ผู้ใช้มีแต้มไม่เพียงพอ")

        available_count = self.env["partner.coupon.code"].sudo().search_count([
            ("coupon_id", "=", self.id),
            ("state", "=", "available"),
        ])
        if not available_count:
            raise ValidationError("คูปองหมดแล้ว")

        if self.max_redeem_per_user > 0:
            redeem_count = self._get_user_redeem_count(user)
            if redeem_count >= self.max_redeem_per_user:
                raise ValidationError("คุณแลกคูปองนี้ครบจำนวนที่กำหนดแล้ว")

    def _get_user_redeem_count(self, user):
        self.ensure_one()
        return self.env["crm.user.coupon"].sudo().search_count([
            ("coupon_id", "=", self.id),
            ("user_id", "=", user.id),
            ("point_id", "!=", False),
        ])

    def _get_user_balance(self, user):
        points = self.env["crm.user.point"].sudo().search([
            ("user_id", "=", user.id),
            ("currency_id", "=", self.currency_id.id),
        ])
        earn = sum(points.filtered(lambda point: point.type == "earn").mapped("value"))
        transfer = sum(points.filtered(lambda point: point.type == "transfer").mapped("value"))
        burn = sum(points.filtered(lambda point: point.type == "burn").mapped("value"))
        return earn - transfer - burn

    @api.constrains("value", "random_range", "code_expiry_interval", "code_source", "max_redeem_per_user")
    def _check_coupon_numbers(self):
        for record in self:
            if record.value < 0:
                raise ValidationError("Coupon value ต้องมากกว่า 0")
            if record.code_source == "generate" and record.random_range <= 0:
                raise ValidationError("Random range ต้องมากกว่า 0")
            if record.code_expiry_interval < 0:
                raise ValidationError("Expiry interval ต้องมากกว่าหรือเท่ากับ 0")
            if record.max_redeem_per_user < 0:
                raise ValidationError("Max redeem per user ต้องมากกว่าหรือเท่ากับ 0")

    @api.constrains("start_time", "end_time")
    def _check_coupon_dates(self):
        for record in self:
            if record.start_time and record.end_time and record.start_time > record.end_time:
                raise ValidationError("Start date ต้องมากกว่า end date.")

    @api.constrains("currency_id", "partner_id")
    def _check_currency_partner(self):
        for record in self:
            if (
                record.currency_id
                and record.partner_id
                and record.currency_id.partner_id != record.partner_id
            ):
                raise ValidationError("Coupon currency อยู่นอกเหนือ Application นี้")
