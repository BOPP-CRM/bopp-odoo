from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request

import json

from ....util.portal_auth import get_authenticated_portal_user, get_portal_admin_from_request
from ....util.request import json_response, xlsx_response


class PortalSalesController(http.Controller):
    @http.route("/api/portal/sales", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def list_sales(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        domain = [("partner_id", "=", partner.id)]

        status = (kwargs.get("status") or "").strip().lower()
        if status in {"paid", "void"}:
            domain.append(("status", "=", status))

        source = (kwargs.get("source") or "").strip().lower()
        if source in {"zortout", "omisell", "receipt", "manual", "other"}:
            domain.append(("source", "=", source))

        user_id = self._parse_int(kwargs.get("user_id"))
        if user_id:
            domain.append(("user_id", "=", user_id))

        search_term = (kwargs.get("search") or "").strip()
        if search_term:
            domain += [
                "|", "|", "|",
                ("order_number", "ilike", search_term),
                ("external_id", "ilike", search_term),
                ("customer_name", "ilike", search_term),
                ("user_id.display_name", "ilike", search_term),
            ]

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        sale_model = request.env["partner.sale"].sudo()
        sales = sale_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="order_date desc, id desc",
        )
        total = sale_model.search_count(domain)

        return json_response({
            "sales": [self._serialize_sale(sale, include_lines=False) for sale in sales],
            "total": total,
        })

    @http.route(
        "/api/portal/sales/export",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def export_sales(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        year = self._parse_int(kwargs.get("year"))
        month = self._parse_int(kwargs.get("month"))
        if not year or not month:
            return json_response(
                {
                    "error": "invalid_params",
                    "message": "กรุณาระบุ year และ month ให้ครบถ้วน",
                },
                status=400,
            )

        sale_model = request.env["partner.sale"].sudo()
        try:
            sale_model._validate_export_month(year, month)
            content = sale_model.build_monthly_export_xlsx(
                portal_user.crm_partner_id,
                year,
                month,
            )
            filename = sale_model.get_export_filename(year, month)
        except ValueError:
            return json_response(
                {
                    "error": "invalid_params",
                    "message": "เดือนหรือปีที่เลือกไม่ถูกต้อง",
                },
                status=400,
            )

        return xlsx_response(content, filename)

    @http.route("/api/portal/sales/<int:sale_id>", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_sale(self, sale_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        sale_response = self._get_sale(portal_user.crm_partner_id, sale_id)
        if sale_response["error"]:
            return sale_response["error"]

        return json_response({
            "sale": self._serialize_sale(sale_response["sale"], include_lines=True),
        })

    @http.route(
        "/api/portal/sales/sync-zortout/status",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_zortout_sale_sync_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.zortout.sale.sync.job"].sudo()
        missing_count = job_model.count_missing_sales_for_partner(partner)
        zortout_configured = bool(
            partner.zortout_enabled
            and partner.zortout_api_key
            and partner.zortout_api_secret
            and partner.zortout_store_name
        )

        return json_response({
            "missing_count": missing_count,
            "zortout_configured": zortout_configured,
            "active_job": job_model.get_active_job_for_partner(partner),
        })

    @http.route(
        "/api/portal/sales/sync-zortout",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def start_zortout_sale_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.zortout.sale.sync.job"].sudo()
        try:
            job = job_model.start_sync_for_partner(partner)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "job": job.serialize_for_portal(),
            "message": "เริ่ม sync รายการขายจาก Zortout แล้ว",
        }, status=201)

    @http.route(
        "/api/portal/sales/sync-zortout/active",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_active_zortout_sale_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.zortout.sale.sync.job"].sudo()
        return json_response({
            "job": job_model.get_active_job_for_partner(partner),
        })

    @http.route(
        "/api/portal/sales/sync-zortout/<int:job_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_zortout_sale_sync_job(self, job_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job = request.env["partner.zortout.sale.sync.job"].sudo().search([
            ("id", "=", job_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not job:
            return json_response(
                {"error": "job_not_found", "message": "ไม่พบงาน sync ดังกล่าว"},
                status=404,
            )

        return json_response({
            "job": job.serialize_for_portal(),
        })

    @http.route(
        "/api/portal/sales/sync-receipts/status",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_receipt_sale_sync_status(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.receipt.sale.sync.job"].sudo()
        return json_response({
            "missing_count": job_model.count_missing_sales_for_partner(partner),
            "openai_configured": partner.serialize_openai_status()["configured"],
            "active_job": job_model.get_active_job_for_partner(partner),
        })

    @http.route(
        "/api/portal/sales/sync-receipts",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def start_receipt_sale_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_json_payload()
        if parse_error:
            return parse_error
        confirm_error = self._require_ai_confirmed(payload)
        if confirm_error:
            return confirm_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.receipt.sale.sync.job"].sudo()
        try:
            job = job_model.start_sync_for_partner(partner)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        return json_response({
            "job": job.serialize_for_portal(),
            "message": "เริ่ม sync ใบเสร็จไปรายการขายด้วย AI แล้ว",
        }, status=201)

    @http.route(
        "/api/portal/sales/sync-receipts/active",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_active_receipt_sale_sync(self, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job_model = request.env["partner.receipt.sale.sync.job"].sudo()
        return json_response({
            "job": job_model.get_active_job_for_partner(partner),
        })

    @http.route(
        "/api/portal/sales/sync-receipts/<int:job_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_receipt_sale_sync_job(self, job_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id.sudo()
        job = request.env["partner.receipt.sale.sync.job"].sudo().search([
            ("id", "=", job_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not job:
            return json_response(
                {"error": "job_not_found", "message": "ไม่พบงาน sync ดังกล่าว"},
                status=404,
            )

        return json_response({
            "job": job.serialize_for_portal(),
        })

    @http.route(
        "/api/portal/sales/<int:sale_id>/regenerate-ai",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def regenerate_sale_with_ai(self, sale_id, **kwargs):
        portal_user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_json_payload()
        if parse_error:
            return parse_error
        confirm_error = self._require_ai_confirmed(payload)
        if confirm_error:
            return confirm_error

        partner = portal_user.crm_partner_id.sudo()
        sale_response = self._get_sale(partner, sale_id)
        if sale_response["error"]:
            return sale_response["error"]

        sale = sale_response["sale"]
        if sale.source != "receipt" or not sale.receipt_redeem_id:
            return json_response(
                {
                    "error": "sale_not_allowed",
                    "message": "สามารถ regenerate ได้เฉพาะรายการขายจากใบเสร็จ (AI)",
                },
                status=400,
            )

        receipt = sale.receipt_redeem_id
        sale_model = request.env["partner.sale"].sudo()
        try:
            sale_model.sync_receipt_with_openai(
                partner,
                receipt,
                regenerate=True,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "validation_error", "message": str(error)},
                status=400,
            )

        sale = request.env["partner.sale"].sudo().browse(sale.id)
        return json_response({
            "sale": self._serialize_sale(sale, include_lines=True),
            "message": "สร้างข้อมูลรายการขายจาก AI ใหม่แล้ว",
        })

    def _parse_json_payload(self):
        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except json.JSONDecodeError:
            return None, json_response(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )
        if not isinstance(payload, dict):
            payload = {}
        return payload, None

    def _require_ai_confirmed(self, payload):
        confirmed = payload.get("ai_confirmed")
        if confirmed is True or str(confirmed).lower() in {"true", "1", "yes"}:
            return None
        return json_response(
            {
                "error": "ai_confirmation_required",
                "message": "กรุณายืนยันความถูกต้องของข้อมูลที่ AI สร้างก่อนดำเนินการ",
            },
            status=400,
        )

    def _parse_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _get_sale(self, partner, sale_id):
        sale = request.env["partner.sale"].sudo().search([
            ("id", "=", sale_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not sale:
            return {
                "sale": False,
                "error": json_response(
                    {"error": "sale_not_found", "message": "ไม่พบรายการขายดังกล่าว"},
                    status=404,
                ),
            }

        return {
            "sale": sale,
            "error": False,
        }

    def _serialize_sale(self, sale, include_lines=False):
        user = sale.user_id
        data = {
            "id": sale.id,
            "source": sale.source,
            "source_label": sale.source_label or sale.source,
            "external_id": sale.external_id,
            "order_number": sale.order_number or False,
            "status": sale.status,
            "order_date": fields.Date.to_string(sale.order_date) if sale.order_date else False,
            "discount": sale.discount or 0,
            "vat_amount": sale.vat_amount or 0,
            "amount": sale.amount or 0,
            "payment_amount": sale.payment_amount or 0,
            "payment_status": sale.payment_status or False,
            "customer_name": sale.customer_name or False,
            "customer_phone": sale.customer_phone or False,
            "customer_email": sale.customer_email or False,
            "last_sync_at": fields.Datetime.to_string(sale.last_sync_at) if sale.last_sync_at else False,
            "receipt_redeem_id": sale.receipt_redeem_id.id if sale.receipt_redeem_id else False,
            "ai_generated": bool(sale.ai_generated),
            "ai_generated_at": fields.Datetime.to_string(sale.ai_generated_at)
            if sale.ai_generated_at
            else False,
            "can_regenerate_ai": sale.source == "receipt" and bool(sale.receipt_redeem_id),
            "user": self._serialize_member(user) if user else False,
        }

        if include_lines:
            data["lines"] = [
                {
                    "id": line.id,
                    "external_product_id": line.external_product_id or False,
                    "sku": line.sku or False,
                    "name": line.name,
                    "quantity": line.quantity or 0,
                    "price_per_unit": line.price_per_unit or 0,
                    "discount": line.discount or 0,
                    "total_price": line.total_price or 0,
                }
                for line in sale.line_ids
            ]

        return data

    def _serialize_member(self, user):
        tier = user.tier_id
        return {
            "id": user.id,
            "display_name": user.display_name,
            "line_user_id": user.line_user_id,
            "email": user.email or False,
            "phone": user.phone or False,
            "picture_url": user.picture_url or False,
            "tier": {
                "id": tier.id,
                "code": tier.code,
                "name": tier.name,
            } if tier else False,
        }
