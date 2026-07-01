import re
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import ValidationError

HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


class PartnerAds(models.Model):
    _name = "partner.ads"
    _description = "Partner Ads"
    _inherit = ["s3.image.mixin"]

    title = fields.Text(string="Title")
    action = fields.Char(string="Action")
    message = fields.Text(string="Message")
    image = fields.Char(string="Image")
    image_file = fields.Image(
        string="Image",
        max_width=1920,
        max_height=1920,
        store=False,
        compute="_compute_image_file",
        inverse="_inverse_image_file",
    )
    start_date = fields.Datetime(string="Start Date", tracking=True, default=fields.Datetime.now)
    end_date = fields.Datetime(string="End Date", tracking=True, default=fields.Datetime.now)
    active = fields.Boolean(default=True, tracking=True)

    partner_id = fields.Many2one(
        "partner",
        string="Partner",
        required=True,
        ondelete="cascade",
    )

    def _get_s3_image_config(self):
        return {
            "image": {
                "max_width": 1920,
                "max_height": 1920,
            },
        }

    @api.depends("image")
    def _compute_image_file(self):
        self._compute_s3_image_file("image")

    def _inverse_image_file(self):
        self._inverse_s3_image_file("image")

    @api.constrains("action")
    def _check_action_url(self):
        for record in self:
            action = record.action
            if not action:
                continue

            parsed = urlparse(action)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValidationError("Action must be a valid URL.")
