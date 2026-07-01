import base64
import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

from ....util.portal_auth import get_authenticated_portal_user
from ....util.request import json_response


class PortalReceiptsController(http.Controller):
    @http.route("/api/portal/receipts", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_receipts(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        domain = [("partner_id", "=", partner.id)]

        state = (kwargs.get("state") or "").strip().lower()
        if state in {"pending", "approved", "rejected"}:
            domain.append(("state", "=", state))

        user_id = self._parse_int(kwargs.get("user_id"))
        if user_id:
            domain.append(("user_id", "=", user_id))

        search_term = (kwargs.get("search") or "").strip()
        if search_term:
            domain += [
                "|", "|",
                ("receipt_number", "ilike", search_term),
                ("user_id.display_name", "ilike", search_term),
                ("user_id.line_user_id", "ilike", search_term),
            ]

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        receipt_model = request.env["crm.partner.receipt.redeem"].sudo()
        receipts = receipt_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="submitted_date desc, id desc",
        )
        total = receipt_model.search_count(domain)

        if receipts:
            receipts.read()

        return json_response({
            "receipts": [
                self._serialize_receipt(receipt)
                for receipt in receipts
            ],
            "total": total,
        })

    @http.route("/api/portal/receipts/members/lookup", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def lookup_member(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        query = (kwargs.get("q") or kwargs.get("query") or "").strip()
        if not query:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุข้อมูลสมาชิก"},
                status=400,
            )

        partner = portal_user.crm_partner_id
        receipt_model = request.env["crm.partner.receipt.redeem"].sudo()
        try:
            member = receipt_model.lookup_member(partner, query)
        except ValidationError as error:
            return json_response(
                {"error": "member_lookup_not_allowed", "message": str(error)},
                status=400,
            )

        if not member:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบสมาชิกดังกล่าว"},
                status=404,
            )

        return json_response({
            "user": self._serialize_member(member),
        })

    @http.route("/api/portal/receipts/manual", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def create_manual_receipt(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        user_id, amount, image_data, parse_error = self._parse_manual_payload(partner)
        if parse_error:
            return parse_error

        member = request.env["crm.user"].sudo().search([
            ("id", "=", user_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not member:
            return json_response(
                {"error": "user_not_found", "message": "ไม่พบสมาชิกดังกล่าว"},
                status=404,
            )

        receipt_model = request.env["crm.partner.receipt.redeem"].sudo()
        try:
            receipt = receipt_model.submit_manual_receipt(
                partner,
                member,
                amount,
                image_data,
            )
            receipt.with_user(portal_user).sudo().action_approve()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "receipt_not_allowed", "message": str(error)},
                status=400,
            )

        receipt.read()
        return json_response({
            "receipt": self._serialize_receipt(receipt),
        }, status=201)

    @http.route("/api/portal/receipts/<int:receipt_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_receipt(self, receipt_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        receipt_response = self._get_receipt(portal_user.crm_partner_id, receipt_id)
        if receipt_response["error"]:
            return receipt_response["error"]

        receipt = receipt_response["receipt"]
        receipt.read()

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        })

    @http.route("/api/portal/receipts/<int:receipt_id>", type="http", auth="public", methods=["PUT"], csrf=False, cors="*")
    def update_receipt(self, receipt_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        receipt_response = self._get_receipt(portal_user.crm_partner_id, receipt_id)
        if receipt_response["error"]:
            return receipt_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        receipt = receipt_response["receipt"]
        if receipt.state != "pending":
            return json_response(
                {"error": "receipt_not_allowed", "message": "แก้ไขได้เฉพาะรายการที่รอตรวจสอบ"},
                status=400,
            )

        vals = {}
        if "amount" in payload:
            vals["amount"] = payload["amount"]
        if "reject_reason" in payload:
            vals["reject_reason"] = payload["reject_reason"]

        if not vals:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            receipt.sudo().write(vals)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "receipt_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        })

    @http.route("/api/portal/receipts/<int:receipt_id>/approve", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def approve_receipt(self, receipt_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        receipt_response = self._get_receipt(portal_user.crm_partner_id, receipt_id)
        if receipt_response["error"]:
            return receipt_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        receipt = receipt_response["receipt"]
        try:
            if "amount" in payload:
                if receipt.state != "pending":
                    raise ValidationError("แก้ไขมูลค่าได้เฉพาะรายการที่รอตรวจสอบ")
                receipt.sudo().write({"amount": payload["amount"]})

            receipt.with_user(portal_user).sudo().action_approve()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "receipt_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        })

    @http.route("/api/portal/receipts/<int:receipt_id>/reject", type="http", auth="public", methods=["POST"], csrf=False, cors="*")
    def reject_receipt(self, receipt_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        receipt_response = self._get_receipt(portal_user.crm_partner_id, receipt_id)
        if receipt_response["error"]:
            return receipt_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        receipt = receipt_response["receipt"]
        try:
            if payload.get("reject_reason") is not None:
                receipt.sudo().write({"reject_reason": payload.get("reject_reason")})

            receipt.with_user(portal_user).sudo().action_reject()
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "receipt_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "receipt": self._serialize_receipt(receipt),
        })

    def _parse_manual_payload(self, partner):
        require_image = partner.portal_manual_receipt_require_image
        upload = request.httprequest.files
        if upload:
            user_id = self._parse_int(
                request.httprequest.form.get("userId")
                or request.httprequest.form.get("user_id")
            )
            amount_raw = (
                request.httprequest.form.get("amount")
                or request.httprequest.form.get("receiptAmount")
            )
            image_file = upload.get("receiptImage") or upload.get("receipt_image")
            if not user_id:
                return None, None, None, json_response(
                    {"error": "invalid_request", "message": "กรุณาระบุสมาชิก"},
                    status=400,
                )
            if require_image and not image_file:
                return None, None, None, json_response(
                    {"error": "invalid_request", "message": "กรุณาอัปโหลดรูปใบเสร็จ"},
                    status=400,
                )

            try:
                amount = float(amount_raw or 0)
            except (TypeError, ValueError):
                amount = 0

            image_data = (
                base64.b64encode(image_file.read())
                if image_file
                else False
            )
            return user_id, amount, image_data, None

        payload, parse_error = self._parse_payload()
        if parse_error:
            return None, None, None, parse_error

        user_id = self._parse_int(payload.get("user_id") or payload.get("userId"))
        amount_raw = payload.get("amount")
        image_data = payload.get("receipt_image") or payload.get("receiptImage")

        if not user_id:
            return None, None, None, json_response(
                {"error": "invalid_request", "message": "กรุณาระบุสมาชิก"},
                status=400,
            )
        if require_image and not image_data:
            return None, None, None, json_response(
                {"error": "invalid_request", "message": "กรุณาอัปโหลดรูปใบเสร็จ"},
                status=400,
            )

        try:
            amount = float(amount_raw or 0)
        except (TypeError, ValueError):
            amount = 0

        return user_id, amount, image_data or False, None

    def _parse_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )

        return payload, None

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get_receipt(self, partner, receipt_id):
        receipt = request.env["crm.partner.receipt.redeem"].sudo().search([
            ("id", "=", receipt_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not receipt:
            return {
                "receipt": False,
                "error": json_response(
                    {"error": "receipt_not_found", "message": "ไม่พบใบเสร็จดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "receipt": receipt,
            "error": False,
        }

    def _serialize_receipt(self, receipt):
        tier = receipt.user_tier_id or receipt.user_id.tier_id
        reviewed_by = receipt.reviewed_by_id
        user = receipt.user_id

        return {
            "id": receipt.id,
            "receipt_number": receipt.receipt_number,
            "receipt_image_url": receipt.receipt_image or False,
            "amount": receipt.amount or 0,
            "state": receipt.state,
            "reject_reason": receipt.reject_reason or False,
            "submitted_date": fields.Datetime.to_string(receipt.submitted_date),
            "reviewed_date": fields.Datetime.to_string(receipt.reviewed_date) if receipt.reviewed_date else False,
            "reviewed_by": {
                "id": reviewed_by.id,
                "name": reviewed_by.name,
            } if reviewed_by else False,
            "tier_convert_points": receipt.tier_convert_points or 0,
            "reward_points": receipt.reward_points or 0,
            "tier": {
                "id": tier.id,
                "code": tier.code,
                "name": tier.name,
                "convert_points": tier.convert_points,
            } if tier else False,
            "user": {
                "id": user.id,
                "display_name": user.display_name,
                "line_user_id": user.line_user_id,
                "email": user.email or False,
                "phone": user.phone or False,
                "picture_url": user.picture_url or False,
            },
            "spending_point": self._serialize_point(receipt.spending_point_id),
            "reward_point": self._serialize_point(receipt.reward_point_id),
        }

    def _serialize_member(self, user):
        tier = user.tier_id
        if tier:
            user._update_tier()
            tier = user.tier_id or tier

        return {
            "id": user.id,
            "display_name": user.display_name,
            "picture_url": user.picture_url or False,
            "line_user_id": user.line_user_id,
            "email": user.email or False,
            "is_email_verified": user.is_email_verified,
            "phone": user.phone or False,
            "is_phone_verified": user.is_phone_verified,
            "birth_date": fields.Date.to_string(user.birth_date) if user.birth_date else False,
            "gender": user.gender or False,
            "tier": {
                "id": tier.id,
                "code": tier.code,
                "name": tier.name,
                "convert_points": tier.convert_points,
            } if tier else False,
            "points": self._serialize_member_balances(user),
        }

    def _serialize_member_balances(self, user):
        balances = []
        for currency in user.partner_id.currency_ids:
            balances.append({
                "currency": {
                    "id": currency.id,
                    "name": currency.name,
                    "is_default": currency.is_default,
                    "is_total_spending": currency.is_total_spending,
                },
                "balance": user._get_currency_balance(currency),
            })
        return balances

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
