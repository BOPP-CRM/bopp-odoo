import base64
import urllib.request

from odoo import models

from ....util.s3 import upload_image_base64


class S3ImageMixin(models.AbstractModel):
    _name = "s3.image.mixin"
    _description = "S3 Image Upload Mixin"

    def _get_s3_image_config(self):
        return {}

    def _get_s3_image_folder(self, field_name):
        return f"{self._name.replace('.', '/')}/{field_name}"

    def _fetch_image_from_url(self, url):
        if not url:
            return False

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return base64.b64encode(response.read())
        except Exception:
            return False

    def _upload_image_field(self, field_name, image_data):
        if not image_data:
            return False

        settings = self._get_s3_image_config().get(field_name, {})
        return upload_image_base64(
            image_data,
            folder=settings.get("folder") or self._get_s3_image_folder(field_name),
            max_width=settings.get("max_width", 1920),
            max_height=settings.get("max_height", 1920),
        )

    def _compute_s3_image_file(self, field_name):
        file_field = f"{field_name}_file"
        for record in self:
            record[file_field] = record._fetch_image_from_url(record[field_name])

    def _inverse_s3_image_file(self, field_name):
        for record in self:
            file_field = f"{field_name}_file"
            record[field_name] = record._upload_image_field(
                field_name,
                record[file_field],
            )
