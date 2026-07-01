from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PartnerWarranty(models.Model):
    _name = "partner.warranty"
    _description = "Partner Warranty Registration"
    _inherit = ["s3.image.mixin", "mail.thread", "mail.activity.mixin"]
    _order = "submitted_date desc, id desc"

    serial_number = fields.Char(string="Serial Number", required=True, tracking=True)
    receipt_number = fields.Char(string="Receipt Number", required=True, tracking=True)
    purchase_date = fields.Date(string="Purchase Date", required=True, tracking=True)
    receipt_image = fields.Char(string="Receipt Image", tracking=True)
    receipt_image_file = fields.Image(
        string="Receipt Image",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_receipt_image_file",
        inverse="_inverse_receipt_image_file",
    )
    submitted_date = fields.Datetime(
        string="Submitted Date",
        default=fields.Datetime.now,
        required=True,
        tracking=True,
    )

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "crm.user",
        string="Member",
        required=True,
        ondelete="cascade",
    )
    product_id = fields.Many2one(
        "partner.warranty.product",
        string="Product",
        required=True,
        ondelete="restrict",
    )
    contributor_id = fields.Many2one(
        "partner.warranty.contributor",
        string="Purchase Channel",
        required=True,
        ondelete="restrict",
    )
    status_id = fields.Many2one(
        "partner.warranty.status",
        string="Status",
        required=True,
        ondelete="restrict",
        tracking=True,
    )
    comment_ids = fields.One2many(
        "partner.warranty.comment",
        "warranty_id",
        string="Comments",
    )

    def _get_s3_image_config(self):
        return {
            "receipt_image": {
                "max_width": 1920,
                "max_height": 1920,
            },
        }

    @api.depends("receipt_image")
    def _compute_receipt_image_file(self):
        self._compute_s3_image_file("receipt_image")

    def _inverse_receipt_image_file(self):
        self._inverse_s3_image_file("receipt_image")

    @api.constrains("user_id", "partner_id")
    def _check_user_partner(self):
        for record in self:
            if (
                record.user_id
                and record.partner_id
                and record.user_id.partner_id != record.partner_id
            ):
                raise ValidationError("สมาชิกต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.constrains("product_id", "contributor_id", "status_id", "partner_id")
    def _check_related_partner(self):
        for record in self:
            for related in (
                record.product_id,
                record.contributor_id,
                record.status_id,
            ):
                if related and related.partner_id != record.partner_id:
                    raise ValidationError("ข้อมูลที่เลือกต้องอยู่ภายใต้ Partner เดียวกัน")

    @api.model
    def _ensure_warranty_enabled(self, partner):
        if not partner.ui_warranty_enabled:
            raise ValidationError("ระบบลงทะเบียนรับประกันสินค้ายังไม่เปิดใช้งาน")

    @api.model
    def _resolve_product(self, partner, product_id):
        product = self.env["partner.warranty.product"].sudo().search([
            ("id", "=", product_id),
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], limit=1)
        if not product:
            raise ValidationError("ไม่พบสินค้าที่เลือก")
        return product

    @api.model
    def _resolve_contributor(self, partner, contributor_id):
        contributor = self.env["partner.warranty.contributor"].sudo().search([
            ("id", "=", contributor_id),
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], limit=1)
        if not contributor:
            raise ValidationError("ไม่พบช่องทางการซื้อสินค้าที่เลือก")
        return contributor

    @api.model
    def _prepare_submission_vals(self, partner, user, payload):
        self._ensure_warranty_enabled(partner)

        serial_number = (payload.get("serial_number") or payload.get("serialNumber") or "").strip()
        receipt_number = (payload.get("receipt_number") or payload.get("receiptNumber") or "").strip()
        purchase_date = payload.get("purchase_date") or payload.get("purchaseDate")
        product_id = payload.get("product_id") or payload.get("productId")
        contributor_id = payload.get("contributor_id") or payload.get("contributorId")
        image_data = payload.get("receipt_image") or payload.get("receiptImage")

        if not serial_number:
            raise ValidationError("กรุณาระบุ Serial Number")
        if not receipt_number:
            raise ValidationError("กรุณาระบุหมายเลขใบเสร็จรับเงิน")
        if not purchase_date:
            raise ValidationError("กรุณาระบุวันที่ซื้อสินค้า")
        if not product_id:
            raise ValidationError("กรุณาเลือกสินค้า")
        if not contributor_id:
            raise ValidationError("กรุณาเลือกช่องทางการซื้อสินค้า")
        if not image_data:
            raise ValidationError("กรุณาอัปโหลดรูปใบเสร็จ")

        product = self._resolve_product(partner, int(product_id))
        contributor = self._resolve_contributor(partner, int(contributor_id))
        status = self.env["partner.warranty.status"].get_default_status(partner)
        if not status:
            raise ValidationError("ยังไม่ได้ตั้งค่าสถานะการรับประกัน")

        receipt_image_url = self._upload_image_field("receipt_image", image_data)

        return {
            "serial_number": serial_number,
            "receipt_number": receipt_number,
            "purchase_date": purchase_date,
            "receipt_image": receipt_image_url,
            "partner_id": partner.id,
            "user_id": user.id,
            "product_id": product.id,
            "contributor_id": contributor.id,
            "status_id": status.id,
            "submitted_date": fields.Datetime.now(),
        }

    @api.model
    def submit_warranty(self, partner, user, payload):
        vals = self._prepare_submission_vals(partner, user, payload)
        return self.create(vals)

    @api.model
    def submit_warranties(self, partner, user, payloads):
        if not payloads:
            raise ValidationError("กรุณาระบุข้อมูลสินค้าที่ต้องการลงทะเบียน")

        records = self.env["partner.warranty"]
        for payload in payloads:
            vals = self._prepare_submission_vals(partner, user, payload)
            records |= self.create(vals)
        return records

    def add_portal_comment(self, portal_user, body):
        self.ensure_one()
        body = (body or "").strip()
        if not body:
            raise ValidationError("กรุณาระบุข้อความ")

        comment = self.env["partner.warranty.comment"].create({
            "warranty_id": self.id,
            "body": body,
            "author_id": portal_user.id,
            "author_name": portal_user.name,
        })
        self.message_post(
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )
        return comment

    def update_status(self, status):
        self.ensure_one()
        if not status or status.partner_id != self.partner_id:
            raise ValidationError("สถานะไม่ถูกต้อง")
        if not status.active:
            raise ValidationError("ไม่สามารถใช้สถานะที่ปิดใช้งาน")

        self.write({"status_id": status.id})
