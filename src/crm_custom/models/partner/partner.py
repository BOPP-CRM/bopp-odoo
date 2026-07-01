import re
import os
from odoo import api, fields, models
from odoo.exceptions import ValidationError

HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


class Inventory(models.Model):
    _name = "partner"
    _description = "Partner"
    _inherit = ["s3.image.mixin", "mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _sql_constraints = [
        ("partner_slug_uniq", "unique(slug)", "Slug must be unique."),
    ]

    name = fields.Char(string="Name", tracking=True, required=True)
    slug = fields.Char(string="Slug", tracking=True, required=True)
    logo = fields.Char(string="Logo", tracking=True)
    logo_file = fields.Image(
        string="Logo",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_logo_file",
        inverse="_inverse_logo_file",
    )

    partner_line_liff_id = fields.Char(string="LIFF ID", tracking=True)
    partner_line_channel_access_token = fields.Char(string="Channel Access Token", password="True" ,tracking=True)

    description = fields.Text(string="Description", tracking=True)

    ui_banner = fields.Char(string="Banner", tracking=True)
    ui_banner_file = fields.Image(
        string="Banner",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_ui_banner_file",
        inverse="_inverse_ui_banner_file",
    )
    ui_background_color = fields.Char(string="Background Color", tracking=True)
    ui_background_white_color = fields.Char(string="Background White Color", tracking=True)
    ui_primary_color = fields.Char(string="Primary Color", tracking=True)
    ui_secondary_color = fields.Char(string="Secondary Color", tracking=True)
    ui_surface_color = fields.Char(string="Surface Color", tracking=True)

    ui_text_color = fields.Char(string="Text Color", tracking=True)
    ui_text_white_color = fields.Char(string="Text White Color", tracking=True)
    ui_text_gray_color = fields.Char(string="Text Gray Color", tracking=True)
    ui_text_success_color = fields.Char(string="Text Success Color", tracking=True)
    ui_text_error_color = fields.Char(string="Text Error Color", tracking=True)

    ui_button_color = fields.Char(string="Button Color", tracking=True)
    ui_button_text_color = fields.Char(string="Button Text Color", tracking=True)

    ui_welcome_title = fields.Char(string="Welcome Title", tracking=True)

    ui_crm_required_phone = fields.Boolean(string="Require Phone", default=False, tracking=True)
    ui_crm_required_email = fields.Boolean(string="Require Email", default=False, tracking=True)
    portal_manual_receipt_require_image = fields.Boolean(
        string="Require Receipt Image (Portal Manual)",
        default=True,
        tracking=True,
        help="If enabled, portal staff must upload a receipt image when creating manual receipts.",
    )
    ui_warranty_enabled = fields.Boolean(
        string="Enable Warranty Registration",
        default=False,
        tracking=True,
        help="If enabled, members can register product warranties and the warranty tab appears in the partner portal.",
    )
    ui_custom_field_ids = fields.One2many(
        "partner.custom.field",
        "partner_id",
        string="Custom Field",
        tracking=True,
    )

    user_ids = fields.One2many(
        "crm.user",
        "partner_id",
        string="Users",
    )
    user_search = fields.Char(string="Search Users")
    filtered_user_ids = fields.Many2many(
        "crm.user",
        string="Filtered Users",
        compute="_compute_filtered_user_ids",
    )

    currency_ids = fields.One2many(
        "crm.partner.currency",
        "partner_id",
        string="Currencies",
    )

    point_redeem_ids = fields.One2many(
        "crm.partner.point.redeem",
        "partner_id",
        string="Point Redeems",
    )

    receipt_redeem_ids = fields.One2many(
        "crm.partner.receipt.redeem",
        "partner_id",
        string="Receipt Redeems",
    )

    warranty_product_ids = fields.One2many(
        "partner.warranty.product",
        "partner_id",
        string="Warranty Products",
    )
    warranty_contributor_ids = fields.One2many(
        "partner.warranty.contributor",
        "partner_id",
        string="Warranty Purchase Channels",
    )
    warranty_status_ids = fields.One2many(
        "partner.warranty.status",
        "partner_id",
        string="Warranty Statuses",
    )
    warranty_ids = fields.One2many(
        "partner.warranty",
        "partner_id",
        string="Warranty Registrations",
    )

    portal_user_ids = fields.One2many(
        "res.users",
        "crm_partner_id",
        string="Portal Users",
        domain=[("is_partner_portal", "=", True)],
    )
    api_monthly_limit = fields.Integer(
        string="API Monthly Limit",
        default=0,
        tracking=True,
        help="Maximum API key requests per month for this partner. 0 = unlimited.",
    )
    api_usage_current_month = fields.Integer(
        string="API Usage (Current Month)",
        compute="_compute_api_usage_current_month",
    )
    api_usage_ids = fields.One2many(
        "partner.portal.api.usage",
        "partner_id",
        string="API Usage History",
    )

    coupon_ids = fields.One2many(
        "partner.coupon",
        "partner_id",
        string="Coupons",
    )

    tier_ids = fields.One2many(
        "partner.tier",
        "partner_id",
        string="Tiers",
    )

    join_reward_ids = fields.One2many(
        "partner.member.reward",
        "partner_id",
        string="Join Rewards",
        domain=[("event", "=", "join")],
    )

    ads_ids = fields.One2many(
        "partner.ads",
        "partner_id",
        string="Ads",
    )

    active = fields.Boolean(string="Active", default=True, tracking=True)

    liff_setup_url = fields.Char(
        string="LIFF Setup URL",
        compute="_compute_liff_setup_url",
        sanitize=False,
    )

    def _get_s3_image_config(self):
        return {
            "logo": {
                "max_width": 1920,
                "max_height": 1920,
            },
            "ui_banner": {
                "max_width": 1920,
                "max_height": 1920,
            },
        }

    @api.depends("logo")
    def _compute_logo_file(self):
        self._compute_s3_image_file("logo")

    def _inverse_logo_file(self):
        self._inverse_s3_image_file("logo")

    @api.depends("ui_banner")
    def _compute_ui_banner_file(self):
        self._compute_s3_image_file("ui_banner")

    def _inverse_ui_banner_file(self):
        self._inverse_s3_image_file("ui_banner")

    @api.depends("user_ids", "user_search", "user_ids.display_name", "user_ids.line_user_id", "user_ids.email", "user_ids.phone")
    def _compute_filtered_user_ids(self):
        for partner in self:
            users = partner.user_ids
            search_term = (partner.user_search or "").strip().lower()
            if search_term:
                users = users.filtered(
                    lambda user: search_term in (user.display_name or "").lower()
                    or search_term in (user.line_user_id or "").lower()
                    or search_term in (user.email or "").lower()
                    or search_term in (user.phone or "").lower()
                )
            partner.filtered_user_ids = users

    def _compute_api_usage_current_month(self):
        usage_model = self.env["partner.portal.api.usage"]
        for partner in self:
            partner.api_usage_current_month = usage_model.get_current_month_count(partner)

    @api.depends("slug")
    def _compute_liff_setup_url(self):
        for record in self:
            frontend_path = os.getenv("FRONTEND_PATH")
            if not record.slug:
                record.liff_setup_url = False
                continue

            record.liff_setup_url = f"{frontend_path}/{record.slug}"

    def action_generate_redeem(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Generate Redeem QR",
            "res_model": "crm.user.point.redeem.generate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.id,
            },
        }

    def action_generate_coupon(self):
        self.ensure_one()
        currency = self.currency_ids.filtered("is_default")[:1] or self.currency_ids[:1]
        return {
            "type": "ir.actions.act_window",
            "name": "Create Coupon",
            "res_model": "partner.coupon.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.id,
                "default_currency_id": currency.id,
            },
        }

    def action_add_portal_user(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Add Portal User",
            "res_model": "partner.portal.user.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_partner_id": self.id,
            },
        }

    def action_open_receipt_redeems(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "crm_custom.action_crm_partner_receipt_redeem"
        )
        action.update({
            "name": f"คะแนนจากใบเสร็จ {self.name}",
            "domain": [("partner_id", "=", self.id)],
            "context": {
                "default_partner_id": self.id,
                "search_default_filter_pending": 1,
            },
            "target": "current",
        })
        return action

    def action_open_warranties(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "crm_custom.action_partner_warranty"
        )
        action.update({
            "name": f"ลงทะเบียนรับประกัน {self.name}",
            "domain": [("partner_id", "=", self.id)],
            "context": {
                "default_partner_id": self.id,
            },
            "target": "current",
        })
        return action

    def ensure_warranty_defaults(self):
        product_model = self.env["partner.warranty.product"]
        contributor_model = self.env["partner.warranty.contributor"]
        status_model = self.env["partner.warranty.status"]

        for partner in self:
            product_model.ensure_default_items(partner)
            contributor_model.ensure_default_items(partner)
            status_model.ensure_default_statuses(partner)

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)

        for partner in partners:
            default_currency = partner.currency_ids.filtered("is_default")
            if not default_currency:
                self.env["crm.partner.currency"].create({
                    "name": "point",
                    "is_default": True,
                    "partner_id": partner.id,
                    "is_total_spending": False,
                })

                self.env["crm.partner.currency"].create({
                    "name": "total spending",
                    "is_default": False,
                    "partner_id": partner.id,
                    "is_total_spending": True,
                })

            if not partner.tier_ids:
                self.env["partner.tier"].create({
                    "name": "Member",
                    "code": "member",
                    "min_spending": 0,
                    "max_spending": 999999999,
                    "convert_points": 25,
                    "partner_id": partner.id,
                })

            partner.ensure_warranty_defaults()

        return partners

    @api.constrains("api_monthly_limit")
    def _check_api_monthly_limit(self):
        for partner in self:
            if partner.api_monthly_limit < 0:
                raise ValidationError("API monthly limit cannot be negative.")

    @api.constrains("ui_background_color", "ui_button_color", "ui_text_color", "ui_button_text_color",  "ui_success_color", "ui_error_color")
    def _check_hex_color_fields(self):
        color_field_names = [
            name
            for name, field in self._fields.items()
            if name.endswith("_color") and field.type == "char"
        ]

        for record in self:
            for field_name in color_field_names:
                color = record[field_name]
                if color and not HEX_COLOR_PATTERN.match(color):
                    label = record._fields[field_name].string
                    raise ValidationError(
                        f"{label} must start with # followed by 6 hexadecimal digits."
                    )
