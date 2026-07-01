import base64
import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.request import json_response


class ReceiptRedeemController(http.Controller):
    @http.route("/api/partner/<string:slug>/user/<string:user_id>/receipt", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def submit_receipt(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        receipt_number, image_data, parse_error = self._parse_submit_payload()
        if parse_error:
            return parse_error

        try:
            receipt = request.env["crm.partner.receipt.redeem"].sudo().submit_receipt(
                user_response["partner"],
                user_response["user"],
                receipt_number,
                image_data,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "receipt_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        }, status=201)

    @http.route( "/api/partner/<string:slug>/user/<string:user_id>/receipt", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_receipts(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        receipts = request.env["crm.partner.receipt.redeem"].sudo().search([
            ("partner_id", "=", user_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
        ], order="submitted_date desc, id desc")

        return json_response({
            "receipts": [self._serialize_receipt(receipt) for receipt in receipts],
        })

    @http.route( "/api/partner/<string:slug>/user/<string:user_id>/receipt/<int:receipt_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_receipt(self, slug, user_id, receipt_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        receipt = request.env["crm.partner.receipt.redeem"].sudo().search([
            ("id", "=", receipt_id),
            ("partner_id", "=", user_response["partner"].id),
            ("user_id", "=", user_response["user"].id),
        ], limit=1)
        if not receipt:
            return json_response(
                {"error": "receipt_not_found", "message": "ไม่พบใบเสร็จดังกล่าว"},
                status=404,
            )

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        })

    def _parse_submit_payload(self):
        upload = request.httprequest.files
        if upload:
            receipt_number = (
                request.httprequest.form.get("receiptNumber")
                or request.httprequest.form.get("receipt_number")
            )
            image_file = upload.get("receiptImage") or upload.get("receipt_image")
            if not image_file:
                return None, None, json_response(
                    {"error": "invalid_request", "message": "กรุณาอัปโหลดรูปใบเสร็จ"},
                    status=400,
                )

            return receipt_number, base64.b64encode(image_file.read()), None

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        receipt_number = payload.get("receiptNumber") or payload.get("receipt_number")
        image_data = payload.get("receiptImage") or payload.get("receipt_image")
        return receipt_number, image_data, None

    def _get_user(self, slug, user_id):
        partner = request.env["partner"].sudo().search([("slug", "=", slug)], limit=1)
        if not partner:
            return {
                "partner": False,
                "user": False,
                "error": json_response(
                    {
                        "error": "partner_not_found",
                        "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่",
                    },
                    status=404,
                ),
            }

        user = request.env["crm.user"].sudo().search([
            ("line_user_id", "=", user_id),
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

        return {
            "partner": partner,
            "user": user,
            "error": False,
        }

    def _serialize_receipt(self, receipt):
        tier = receipt.user_id.tier_id
        return {
            "id": receipt.id,
            "receipt_number": receipt.receipt_number,
            "receipt_image_url": receipt.receipt_image or False,
            "amount": receipt.amount or 0,
            "state": receipt.state,
            "reject_reason": receipt.reject_reason or False,
            "submitted_date": fields.Datetime.to_string(receipt.submitted_date),
            "reviewed_date": fields.Datetime.to_string(receipt.reviewed_date),
            "reward_points": receipt.reward_points,
            "tier": {
                "code": tier.code,
                "name": tier.name,
                "convert_points": tier.convert_points,
            } if tier else False,
            "spending_point": self._serialize_point(receipt.spending_point_id),
            "reward_point": self._serialize_point(receipt.reward_point_id),
        }

    def _serialize_point(self, point):
        if not point:
            return False

        return {
            "id": point.id,
            "name": point.name,
            "value": point.value,
            "type": point.type,
            "given_date": fields.Datetime.to_string(point.given_date),
            "currency": {
                "id": point.currency_id.id,
                "name": point.currency_id.name,
                "is_default": point.currency_id.is_default,
                "is_total_spending": point.currency_id.is_total_spending,
            },
        }
