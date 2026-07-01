import base64
import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.line_auth import get_line_profile_from_request
from ....util.request import json_response


class UserWarrantyController(http.Controller):
    @http.route(
        "/api/partner/<string:slug>/warranty/options",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_warranty_options(self, slug, **kwargs):
        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return partner_response["error"]

        partner = partner_response["partner"]
        if not partner.ui_warranty_enabled:
            return json_response(
                {
                    "error": "warranty_not_enabled",
                    "message": "ระบบลงทะเบียนรับประกันสินค้ายังไม่เปิดใช้งาน",
                },
                status=403,
            )

        return json_response(self._serialize_warranty_options(partner))

    @http.route(
        "/api/partner/<string:slug>/user/warranty",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def submit_warranty(self, slug, **kwargs):
        return self._submit_warranty(slug)

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/warranty",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def submit_warranty_legacy(self, slug, user_id, **kwargs):
        return self._submit_warranty(slug)

    @http.route(
        "/api/partner/<string:slug>/user/warranty",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_warranties(self, slug, **kwargs):
        return self._list_warranties(slug)

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/warranty",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_warranties_legacy(self, slug, user_id, **kwargs):
        return self._list_warranties(slug)

    @http.route(
        "/api/partner/<string:slug>/user/warranty/<int:warranty_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_warranty(self, slug, warranty_id, **kwargs):
        return self._get_warranty(slug, warranty_id)

    @http.route(
        "/api/partner/<string:slug>/user/<string:user_id>/warranty/<int:warranty_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_warranty_legacy(self, slug, user_id, warranty_id, **kwargs):
        return self._get_warranty(slug, warranty_id)

    def _submit_warranty(self, slug):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user_from_token(slug, line_profile)
        if user_response["error"]:
            return user_response["error"]

        partner = user_response["partner"]
        user = user_response["user"]
        payloads, parse_error = self._parse_submit_payload()
        if parse_error:
            return parse_error

        warranty_model = request.env["partner.warranty"].sudo()
        try:
            if len(payloads) == 1:
                warranty = warranty_model.submit_warranty(partner, user, payloads[0])
                return json_response({
                    "warranty": self._serialize_warranty(warranty),
                }, status=201)

            warranties = warranty_model.submit_warranties(partner, user, payloads)
            return json_response({
                "warranties": [
                    self._serialize_warranty(warranty)
                    for warranty in warranties
                ],
            }, status=201)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "warranty_not_allowed", "message": str(error)},
                status=400,
            )

    def _list_warranties(self, slug):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user_from_token(slug, line_profile)
        if user_response["error"]:
            return user_response["error"]

        partner = user_response["partner"]
        user = user_response["user"]
        if not partner.ui_warranty_enabled:
            return json_response({"warranties": []})

        warranties = request.env["partner.warranty"].sudo().search([
            ("partner_id", "=", partner.id),
            ("user_id", "=", user.id),
        ], order="submitted_date desc, id desc")

        return json_response({
            "warranties": [
                self._serialize_warranty(warranty)
                for warranty in warranties
            ],
        })

    def _get_warranty(self, slug, warranty_id):
        line_profile, auth_error = get_line_profile_from_request()
        if auth_error:
            return auth_error

        user_response = self._get_user_from_token(slug, line_profile)
        if user_response["error"]:
            return user_response["error"]

        warranty = request.env["partner.warranty"].sudo().search([
            ("id", "=", warranty_id),
            ("partner_id", "=", user_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
        ], limit=1)
        if not warranty:
            return json_response(
                {"error": "warranty_not_found", "message": "ไม่พบรายการรับประกันดังกล่าว"},
                status=404,
            )

        return json_response({
            "warranty": self._serialize_warranty(warranty),
        })

    def _parse_submit_payload(self):
        upload = request.httprequest.files
        if upload:
            items_raw = request.httprequest.form.get("items") or request.httprequest.form.get("warranties")
            if items_raw:
                try:
                    items = json.loads(items_raw)
                except json.JSONDecodeError:
                    return None, json_response(
                        {"error": "invalid_json", "message": "Invalid items JSON."},
                        status=400,
                    )
            else:
                items = [{
                    "product_id": request.httprequest.form.get("productId")
                    or request.httprequest.form.get("product_id"),
                    "serial_number": request.httprequest.form.get("serialNumber")
                    or request.httprequest.form.get("serial_number"),
                    "receipt_number": request.httprequest.form.get("receiptNumber")
                    or request.httprequest.form.get("receipt_number"),
                    "contributor_id": request.httprequest.form.get("contributorId")
                    or request.httprequest.form.get("contributor_id"),
                    "purchase_date": request.httprequest.form.get("purchaseDate")
                    or request.httprequest.form.get("purchase_date"),
                    "receipt_image_key": "receiptImage",
                }]

            payloads = []
            for index, item in enumerate(items):
                image_key = item.get("receipt_image_key") or f"receiptImage_{index}"
                image_file = upload.get(image_key) or upload.get("receiptImage") or upload.get("receipt_image")
                image_data = base64.b64encode(image_file.read()) if image_file else False
                payloads.append({
                    "product_id": item.get("product_id") or item.get("productId"),
                    "serial_number": item.get("serial_number") or item.get("serialNumber"),
                    "receipt_number": item.get("receipt_number") or item.get("receiptNumber"),
                    "contributor_id": item.get("contributor_id") or item.get("contributorId"),
                    "purchase_date": item.get("purchase_date") or item.get("purchaseDate"),
                    "receipt_image": image_data,
                })
            return payloads, None

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        items = payload.get("warranties") or payload.get("items")
        if items is None:
            items = [payload]

        return items, None

    def _get_partner(self, slug):
        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return {
                "partner": False,
                "error": json_response(
                    {
                        "error": "partner_not_found",
                        "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่",
                    },
                    status=404,
                ),
            }

        return {"partner": partner, "error": False}

    def _get_user_from_token(self, slug, line_profile):
        line_user_id = line_profile.get("userId")
        if not line_user_id:
            return {
                "partner": False,
                "user": False,
                "error": json_response(
                    {"error": "unauthorized", "message": "Invalid LINE access token."},
                    status=401,
                ),
            }

        partner_response = self._get_partner(slug)
        if partner_response["error"]:
            return {
                "partner": False,
                "user": False,
                "error": partner_response["error"],
            }

        partner = partner_response["partner"]
        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", line_user_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not user:
            return {
                "partner": partner,
                "user": False,
                "error": json_response(
                    {"error": "user_not_found", "message": "ไม่พบผู้ใช้งานดังกล่าว"},
                    status=404,
                ),
            }

        return {"partner": partner, "user": user, "error": False}

    def _serialize_warranty_options(self, partner):
        partner.ensure_warranty_defaults()
        products = request.env["partner.warranty.product"].sudo().search([
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], order="name asc, id asc")
        contributors = request.env["partner.warranty.contributor"].sudo().search([
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], order="sequence asc, name asc, id asc")
        statuses = request.env["partner.warranty.status"].sudo().search([
            ("partner_id", "=", partner.id),
            ("active", "=", True),
        ], order="sequence asc, id asc")

        return {
            "enabled": partner.ui_warranty_enabled,
            "products": [self._serialize_product(product) for product in products],
            "contributors": [
                self._serialize_contributor(contributor)
                for contributor in contributors
            ],
            "statuses": [self._serialize_status(status) for status in statuses],
        }

    def _serialize_warranty(self, warranty):
        return {
            "id": warranty.id,
            "serial_number": warranty.serial_number,
            "receipt_number": warranty.receipt_number,
            "purchase_date": fields.Date.to_string(warranty.purchase_date),
            "receipt_image_url": warranty.receipt_image or False,
            "submitted_date": fields.Datetime.to_string(warranty.submitted_date),
            "product": self._serialize_product(warranty.product_id),
            "contributor": self._serialize_contributor(warranty.contributor_id),
            "status": self._serialize_status(warranty.status_id),
        }

    def _serialize_product(self, product):
        return {
            "id": product.id,
            "name": product.name,
            "description": product.description or False,
            "sku": product.sku or False,
            "cost_price": product.cost_price or 0,
            "sell_price": product.sell_price or 0,
            "image_url": product.image or False,
        }

    def _serialize_contributor(self, contributor):
        return {
            "id": contributor.id,
            "name": contributor.name,
        }

    def _serialize_status(self, status):
        return {
            "id": status.id,
            "code": status.code,
            "label": status.label,
            "color": status.color or False,
            "is_default": status.is_default,
        }
