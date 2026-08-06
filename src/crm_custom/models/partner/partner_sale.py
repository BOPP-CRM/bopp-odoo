from odoo import api, fields, models


class PartnerSaleLine(models.Model):
    _name = "partner.sale.line"
    _description = "Partner Sale Line"
    _order = "sequence, id"

    sale_id = fields.Many2one(
        "partner.sale",
        string="Sale",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(string="Sequence", default=10)
    external_product_id = fields.Integer(string="External Product ID")
    sku = fields.Char(string="SKU")
    name = fields.Char(string="Product Name", required=True)
    quantity = fields.Float(string="Quantity", default=1.0)
    price_per_unit = fields.Float(string="Price Per Unit")
    discount = fields.Float(string="Discount")
    total_price = fields.Float(string="Total Price")


class PartnerSale(models.Model):
    _name = "partner.sale"
    _description = "Partner Sale"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "order_date desc, id desc"
    _sql_constraints = [
        (
            "partner_sale_source_external_uniq",
            "unique(partner_id, source, external_id)",
            "Sale record must be unique per partner, source and external reference.",
        ),
    ]

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    source = fields.Selection(
        [
            ("zortout", "Zortout"),
            ("omisell", "Omisell"),
            ("receipt", "Receipt (AI)"),
            ("manual", "Manual"),
            ("other", "Other"),
        ],
        string="Source",
        required=True,
        default="other",
        tracking=True,
    )
    external_id = fields.Char(
        string="External ID",
        required=True,
        index=True,
        tracking=True,
        help="External order identifier from the integration source.",
    )
    order_number = fields.Char(string="Order Number", tracking=True)
    status = fields.Selection(
        [
            ("paid", "Paid"),
            ("void", "Void"),
        ],
        string="Status",
        required=True,
        default="paid",
        tracking=True,
    )
    order_date = fields.Date(string="Order Date", tracking=True)
    discount = fields.Float(string="Discount")
    vat_amount = fields.Float(string="VAT Amount")
    amount = fields.Float(string="Amount", tracking=True)
    payment_amount = fields.Float(string="Payment Amount")
    payment_status = fields.Char(string="Payment Status", tracking=True)
    customer_name = fields.Char(string="Customer Name")
    customer_phone = fields.Char(string="Customer Phone")
    customer_email = fields.Char(string="Customer Email")
    last_sync_at = fields.Datetime(string="Last Sync At", tracking=True)
    user_id = fields.Many2one(
        "crm.user",
        string="Member",
        ondelete="set null",
        tracking=True,
    )
    zortout_order_id = fields.Many2one(
        "partner.zortout.order",
        string="Zortout Order",
        ondelete="set null",
    )
    receipt_redeem_id = fields.Many2one(
        "crm.partner.receipt.redeem",
        string="Receipt Redeem",
        ondelete="set null",
    )
    line_ids = fields.One2many(
        "partner.sale.line",
        "sale_id",
        string="Lines",
    )
    source_label = fields.Char(string="Source Label", compute="_compute_source_label")
    ai_generated = fields.Boolean(string="AI Generated", default=False, tracking=True)
    ai_generated_at = fields.Datetime(string="AI Generated At", tracking=True)

    SOURCE_LABELS = {
        "zortout": "Zortout",
        "omisell": "Omisell",
        "receipt": "ใบเสร็จ (AI)",
        "manual": "Manual",
        "other": "Other",
    }

    @api.depends("source")
    def _compute_source_label(self):
        for record in self:
            record.source_label = self.SOURCE_LABELS.get(record.source, record.source or "")

    @api.model
    def is_zortout_sale_exists(self, partner, zortout_order_id):
        return bool(self.search([
            ("partner_id", "=", partner.id),
            ("source", "=", "zortout"),
            ("external_id", "=", str(int(zortout_order_id))),
        ], limit=1))

    @api.model
    def find_zortout_orders_missing_sale(self, partner):
        order_model = self.env["partner.zortout.order"].sudo()
        paid_orders = order_model.search([
            ("partner_id", "=", partner.id),
            ("payment_status", "=", "Paid"),
        ], order="last_webhook_at asc, id asc")
        if not paid_orders:
            return order_model.browse()

        existing_external_ids = set(
            self.search([
                ("partner_id", "=", partner.id),
                ("source", "=", "zortout"),
                ("external_id", "in", [
                    str(order.zortout_order_id) for order in paid_orders
                ]),
            ]).mapped("external_id")
        )
        return paid_orders.filtered(
            lambda order: str(order.zortout_order_id) not in existing_external_ids
        )

    @api.model
    def find_receipts_missing_sale(self, partner):
        receipt_model = self.env["crm.partner.receipt.redeem"].sudo()
        receipts = receipt_model.search([
            ("partner_id", "=", partner.id),
            ("state", "=", "approved"),
            ("receipt_image", "!=", False),
            ("zortout_order_id", "=", False),
        ], order="reviewed_date asc, id asc")
        if not receipts:
            return receipt_model.browse()

        synced_receipt_ids = set(
            self.search([
                ("partner_id", "=", partner.id),
                ("receipt_redeem_id", "in", receipts.ids),
            ]).mapped("receipt_redeem_id").ids
        )
        return receipts.filtered(lambda receipt: receipt.id not in synced_receipt_ids)

    @api.model
    def _receipt_external_id(self, receipt_id):
        return f"receipt-{int(receipt_id)}"

    @api.model
    def sync_from_receipt_ai(self, partner, receipt, ai_data, regenerate=False):
        receipt.ensure_one()
        external_id = self._receipt_external_id(receipt.id)
        sale = self.search([
            ("partner_id", "=", partner.id),
            ("source", "=", "receipt"),
            ("external_id", "=", external_id),
        ], limit=1)

        if sale and not regenerate:
            return {"status": "skipped", "sale_id": sale.id}

        now = fields.Datetime.now()
        user = receipt.user_id
        line_commands = self._build_receipt_ai_line_commands(partner, ai_data)
        amount = partner._parse_zortout_amount(
            ai_data.get("amount") if ai_data.get("amount") is not None else receipt.amount
        )
        payment_amount_raw = ai_data.get("paymentamount")
        if payment_amount_raw is None:
            payment_amount_raw = ai_data.get("payment_amount")
        payment_amount = partner._parse_zortout_amount(
            payment_amount_raw if payment_amount_raw is not None else amount
        )

        vals = {
            "partner_id": partner.id,
            "source": "receipt",
            "external_id": external_id,
            "order_number": (
                ai_data.get("order_number")
                or ai_data.get("orderNumber")
                or receipt.receipt_number
            ),
            "status": "paid",
            "discount": self._parse_ai_amount(partner, ai_data.get("discount")),
            "vat_amount": self._parse_ai_amount(partner, ai_data.get("vat_amount")),
            "amount": amount,
            "payment_amount": payment_amount,
            "payment_status": ai_data.get("payment_status") or ai_data.get("paymentstatus") or "Paid",
            "customer_name": user.display_name if user else False,
            "customer_phone": user.phone if user else False,
            "customer_email": user.email if user else False,
            "user_id": user.id if user else False,
            "receipt_redeem_id": receipt.id,
            "order_date": self._parse_ai_order_date(ai_data, receipt),
            "last_sync_at": now,
            "ai_generated": True,
            "ai_generated_at": now,
            "line_ids": [(5, 0, 0)] + line_commands,
        }

        if sale:
            sale.write(vals)
        else:
            sale = self.create(vals)

        return {"status": "ok", "sale_id": sale.id}

    @api.model
    def sync_receipt_with_openai(self, partner, receipt, regenerate=False):
        if not regenerate:
            existing = self.search([
                ("partner_id", "=", partner.id),
                ("receipt_redeem_id", "=", receipt.id),
            ], limit=1)
            if existing:
                return {"status": "skipped", "sale_id": existing.id}

        ai_data = partner.extract_receipt_sale_data_with_openai(receipt)
        return self.sync_from_receipt_ai(partner, receipt, ai_data, regenerate=regenerate)

    @api.model
    def _build_receipt_ai_line_commands(self, partner, ai_data):
        lines = ai_data.get("items") or ai_data.get("list") or []
        commands = []
        for index, item in enumerate(lines):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("description") or "Unknown Product"
            commands.append((0, 0, {
                "sequence": (index + 1) * 10,
                "external_product_id": 0,
                "sku": item.get("sku") or False,
                "name": name,
                "quantity": self._parse_ai_amount(partner, item.get("quantity"), default=1.0),
                "price_per_unit": self._parse_ai_amount(
                    partner,
                    item.get("price_per_unit") if item.get("price_per_unit") is not None else item.get("unitPrice"),
                ),
                "discount": self._parse_ai_amount(partner, item.get("discount")),
                "total_price": self._parse_ai_amount(
                    partner,
                    item.get("total_price") if item.get("total_price") is not None else item.get("amount"),
                ),
            }))
        return commands

    @staticmethod
    def _parse_ai_amount(partner, value, default=0.0):
        if value is None or value == "":
            return default
        return partner._parse_zortout_amount(value)

    @staticmethod
    def _parse_ai_order_date(ai_data, receipt):
        order_date = (
            ai_data.get("order_date")
            or ai_data.get("orderDate")
            or ai_data.get("date")
        )
        if order_date:
            try:
                return fields.Date.to_date(str(order_date)[:10])
            except (TypeError, ValueError):
                pass
        if receipt.submitted_date:
            return fields.Date.to_date(receipt.submitted_date)
        if receipt.reviewed_date:
            return fields.Date.to_date(receipt.reviewed_date)
        return False

    @api.model
    def sync_from_zortout_webhook(self, partner, method, payload, zortout_order=None):
        method = (method or "").strip().upper()
        zortout_order_id = payload.get("id")
        if not zortout_order_id:
            return {"status": "ignored", "reason": "missing_order_id"}

        external_id = str(int(zortout_order_id))
        sale = self.search([
            ("partner_id", "=", partner.id),
            ("source", "=", "zortout"),
            ("external_id", "=", external_id),
        ], limit=1)

        now = fields.Datetime.now()

        if method == "DELETEORDER" or partner.is_zortout_order_voided(payload):
            if sale:
                sale.write({"status": "void", "last_sync_at": now})
            return {
                "status": "ok",
                "sale_id": sale.id if sale else False,
                "voided": bool(sale),
            }

        if not partner.is_zortout_payment_successful(payload):
            if sale and sale.status == "paid":
                sale.write({"status": "void", "last_sync_at": now})
            return {
                "status": "ok",
                "sale_id": sale.id if sale else False,
                "skipped": "not_paid",
            }

        user = partner.find_user_from_zortout_payload(payload)
        if zortout_order and zortout_order.user_id:
            user = zortout_order.user_id

        line_commands = self._build_zortout_line_commands(partner, payload)
        vals = {
            "partner_id": partner.id,
            "source": "zortout",
            "external_id": external_id,
            "order_number": payload.get("number") or False,
            "status": "paid",
            "discount": self._parse_optional_amount(partner, payload.get("discount")),
            "vat_amount": self._parse_optional_amount(partner, payload.get("vatamount")),
            "amount": partner._parse_zortout_amount(payload.get("amount")),
            "payment_amount": partner._parse_zortout_amount(payload.get("paymentamount")),
            "payment_status": payload.get("paymentstatus") or "Paid",
            "customer_name": payload.get("customername") or False,
            "customer_phone": payload.get("customerphone") or False,
            "customer_email": payload.get("customeremail") or False,
            "user_id": user.id if user else False,
            "zortout_order_id": zortout_order.id if zortout_order else False,
            "receipt_redeem_id": (
                zortout_order.receipt_redeem_id.id
                if zortout_order and zortout_order.receipt_redeem_id
                else False
            ),
            "order_date": self._parse_zortout_order_date(payload),
            "last_sync_at": now,
            "line_ids": [(5, 0, 0)] + line_commands,
        }

        if sale:
            sale.write(vals)
        else:
            sale = self.create(vals)

        if zortout_order and zortout_order.receipt_redeem_id and not sale.receipt_redeem_id:
            sale.receipt_redeem_id = zortout_order.receipt_redeem_id.id

        return {"status": "ok", "sale_id": sale.id}

    @api.model
    def _build_zortout_line_commands(self, partner, payload):
        lines = payload.get("list") or []
        commands = []
        for index, item in enumerate(lines):
            if not isinstance(item, dict):
                continue
            product_id = item.get("productid")
            commands.append((0, 0, {
                "sequence": (index + 1) * 10,
                "external_product_id": int(product_id) if product_id else 0,
                "sku": item.get("sku") or False,
                "name": item.get("name") or "Unknown Product",
                "quantity": self._parse_optional_amount(partner, item.get("number"), default=1.0),
                "price_per_unit": self._parse_optional_amount(partner, item.get("pricepernumber")),
                "discount": self._parse_optional_amount(partner, item.get("discount")),
                "total_price": self._parse_optional_amount(partner, item.get("totalprice")),
            }))
        return commands

    @staticmethod
    def _parse_optional_amount(partner, value, default=0.0):
        if value is None or value == "":
            return default
        return partner._parse_zortout_amount(value)

    @staticmethod
    def _parse_zortout_order_date(payload):
        order_date_string = (payload.get("orderdateString") or "").strip()
        if order_date_string:
            return fields.Date.to_date(order_date_string)
        return False
