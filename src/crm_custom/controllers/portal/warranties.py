import json

from odoo import fields, http
from odoo.exceptions import ValidationError
from odoo.http import request
from psycopg2 import IntegrityError

from ....util.portal_auth import get_authenticated_portal_user
from ....util.request import json_response


class PortalWarrantiesController(http.Controller):
    @http.route(
        "/api/portal/warranty/config",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_config(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        partner.ensure_warranty_defaults()

        return json_response({
            "enabled": partner.ui_warranty_enabled,
            "products": [
                self._serialize_product(product)
                for product in self._search_products(partner)
            ],
            "contributors": [
                self._serialize_contributor(contributor)
                for contributor in self._search_contributors(partner)
            ],
            "statuses": [
                self._serialize_status(status)
                for status in self._search_statuses(partner)
            ],
        })

    @http.route(
        "/api/portal/warranties",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_warranties(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        domain = [("partner_id", "=", partner.id)]

        status_id = self._parse_int(kwargs.get("status_id") or kwargs.get("statusId"))
        if status_id:
            domain.append(("status_id", "=", status_id))

        user_id = self._parse_int(kwargs.get("user_id") or kwargs.get("userId"))
        if user_id:
            domain.append(("user_id", "=", user_id))

        search_term = (kwargs.get("search") or "").strip()
        if search_term:
            domain += [
                "|", "|", "|",
                ("serial_number", "ilike", search_term),
                ("receipt_number", "ilike", search_term),
                ("user_id.display_name", "ilike", search_term),
                ("product_id.name", "ilike", search_term),
            ]

        limit = self._parse_int(kwargs.get("limit"))
        offset = self._parse_int(kwargs.get("offset")) or 0

        warranty_model = request.env["partner.warranty"].sudo()
        warranties = warranty_model.search(
            domain,
            limit=limit,
            offset=offset,
            order="submitted_date desc, id desc",
        )
        total = warranty_model.search_count(domain)

        return json_response({
            "warranties": [
                self._serialize_warranty(warranty)
                for warranty in warranties
            ],
            "total": total,
        })

    @http.route(
        "/api/portal/warranties/<int:warranty_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def get_warranty(self, warranty_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        warranty_response = self._get_warranty(portal_user.crm_partner_id, warranty_id)
        if warranty_response["error"]:
            return warranty_response["error"]

        return json_response({
            "warranty": self._serialize_warranty(
                warranty_response["warranty"],
                include_comments=True,
            ),
        })

    @http.route(
        "/api/portal/warranties/<int:warranty_id>",
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
        cors="*",
    )
    def update_warranty(self, warranty_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        warranty_response = self._get_warranty(portal_user.crm_partner_id, warranty_id)
        if warranty_response["error"]:
            return warranty_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        warranty = warranty_response["warranty"]
        status_id = payload.get("status_id") or payload.get("statusId")
        if status_id is None:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ status_id"},
                status=400,
            )

        status = request.env["partner.warranty.status"].sudo().search([
            ("id", "=", int(status_id)),
            ("partner_id", "=", portal_user.crm_partner_id.id),
        ], limit=1)
        if not status:
            return json_response(
                {"error": "status_not_found", "message": "ไม่พบสถานะดังกล่าว"},
                status=404,
            )

        try:
            warranty.update_status(status)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "warranty_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "warranty": self._serialize_warranty(warranty, include_comments=True),
        })

    @http.route(
        "/api/portal/warranties/<int:warranty_id>/comments",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def add_comment(self, warranty_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        warranty_response = self._get_warranty(portal_user.crm_partner_id, warranty_id)
        if warranty_response["error"]:
            return warranty_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        body = payload.get("body") or payload.get("comment")
        try:
            comment = warranty_response["warranty"].add_portal_comment(portal_user, body)
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "comment_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "comment": self._serialize_comment(comment),
        }, status=201)

    @http.route(
        "/api/portal/warranty-products",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_products(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        include_inactive = (kwargs.get("include_inactive") or "").lower() in {"1", "true", "yes"}
        products = self._search_products(partner, include_inactive=include_inactive)

        return json_response({
            "products": [self._serialize_product(product) for product in products],
        })

    @http.route(
        "/api/portal/warranty-products",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def create_product(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals, validation_error = self._product_vals_from_payload(
            payload,
            partner_id=portal_user.crm_partner_id.id,
        )
        if validation_error:
            return validation_error

        try:
            product = request.env["partner.warranty.product"].sudo().create(vals)
            image_url = self._resolve_product_image_url(
                portal_user.crm_partner_id,
                payload,
                product,
            )
            if image_url is not None:
                product.write({"image": image_url})
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "product_not_allowed", "message": "ชื่อสินค้านี้มีอยู่แล้ว"},
                status=400,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "product_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "product": self._serialize_product(product),
        }, status=201)

    @http.route(
        "/api/portal/warranty-products/<int:product_id>",
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
        cors="*",
    )
    def update_product(self, product_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        product_response = self._get_product(portal_user.crm_partner_id, product_id)
        if product_response["error"]:
            return product_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = {}
        for field in ("name", "description", "sku", "cost_price", "sell_price", "active"):
            camel = self._to_camel(field)
            if field in payload or camel in payload:
                vals[field] = payload.get(field, payload.get(camel))

        if "name" in vals and not (vals["name"] or "").strip():
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุชื่อสินค้า"},
                status=400,
            )

        try:
            image_url = self._resolve_product_image_url(
                portal_user.crm_partner_id,
                payload,
                product_response["product"],
            )
            if image_url is not None:
                vals["image"] = image_url
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "product_not_allowed", "message": str(error)},
                status=400,
            )

        if not vals:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            product_response["product"].write(vals)
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "product_not_allowed", "message": "ชื่อสินค้านี้มีอยู่แล้ว"},
                status=400,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "product_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "product": self._serialize_product(product_response["product"]),
        })

    @http.route(
        "/api/portal/warranty-products/<int:product_id>",
        type="http",
        auth="public",
        methods=["DELETE"],
        csrf=False,
        cors="*",
    )
    def delete_product(self, product_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        product_response = self._get_product(portal_user.crm_partner_id, product_id)
        if product_response["error"]:
            return product_response["error"]

        product_response["product"].write({"active": False})
        return json_response({"success": True})

    @http.route(
        "/api/portal/warranty-contributors",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_contributors(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        include_inactive = (kwargs.get("include_inactive") or "").lower() in {"1", "true", "yes"}
        contributors = self._search_contributors(partner, include_inactive=include_inactive)

        return json_response({
            "contributors": [
                self._serialize_contributor(contributor)
                for contributor in contributors
            ],
        })

    @http.route(
        "/api/portal/warranty-contributors",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def create_contributor(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        name = (payload.get("name") or "").strip()
        if not name:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุชื่อช่องทางการขาย"},
                status=400,
            )

        vals = {
            "name": name,
            "partner_id": portal_user.crm_partner_id.id,
            "sequence": payload.get("sequence") or 10,
            "active": payload.get("active", True),
        }

        try:
            contributor = request.env["partner.warranty.contributor"].sudo().create(vals)
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "contributor_not_allowed", "message": "ชื่อช่องทางนี้มีอยู่แล้ว"},
                status=400,
            )

        return json_response({
            "contributor": self._serialize_contributor(contributor),
        }, status=201)

    @http.route(
        "/api/portal/warranty-contributors/<int:contributor_id>",
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
        cors="*",
    )
    def update_contributor(self, contributor_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        contributor_response = self._get_contributor(
            portal_user.crm_partner_id,
            contributor_id,
        )
        if contributor_response["error"]:
            return contributor_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = {}
        if "name" in payload:
            name = (payload.get("name") or "").strip()
            if not name:
                return json_response(
                    {"error": "invalid_request", "message": "กรุณาระบุชื่อช่องทางการขาย"},
                    status=400,
                )
            vals["name"] = name
        if "sequence" in payload:
            vals["sequence"] = payload["sequence"]
        if "active" in payload:
            vals["active"] = payload["active"]

        if not vals:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            contributor_response["contributor"].write(vals)
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "contributor_not_allowed", "message": "ชื่อช่องทางนี้มีอยู่แล้ว"},
                status=400,
            )

        return json_response({
            "contributor": self._serialize_contributor(contributor_response["contributor"]),
        })

    @http.route(
        "/api/portal/warranty-contributors/<int:contributor_id>",
        type="http",
        auth="public",
        methods=["DELETE"],
        csrf=False,
        cors="*",
    )
    def delete_contributor(self, contributor_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        contributor_response = self._get_contributor(
            portal_user.crm_partner_id,
            contributor_id,
        )
        if contributor_response["error"]:
            return contributor_response["error"]

        contributor_response["contributor"].write({"active": False})
        return json_response({"success": True})

    @http.route(
        "/api/portal/warranty-statuses",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        cors="*",
    )
    def list_statuses(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        partner = portal_user.crm_partner_id
        partner.ensure_warranty_defaults()
        include_inactive = (kwargs.get("include_inactive") or "").lower() in {"1", "true", "yes"}
        statuses = self._search_statuses(partner, include_inactive=include_inactive)

        return json_response({
            "statuses": [self._serialize_status(status) for status in statuses],
        })

    @http.route(
        "/api/portal/warranty-statuses",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        cors="*",
    )
    def create_status(self, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        code = (payload.get("code") or "").strip()
        label = (payload.get("label") or "").strip()
        if not code:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ code"},
                status=400,
            )
        if not label:
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ label"},
                status=400,
            )

        partner = portal_user.crm_partner_id
        vals = {
            "code": code,
            "label": label,
            "partner_id": partner.id,
            "sequence": payload.get("sequence") or 10,
            "color": payload.get("color") or False,
            "is_default": bool(payload.get("is_default") or payload.get("isDefault")),
            "active": payload.get("active", True),
        }

        try:
            status = request.env["partner.warranty.status"].sudo().create(vals)
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "status_not_allowed", "message": "code นี้มีอยู่แล้ว"},
                status=400,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "status_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "status": self._serialize_status(status),
        }, status=201)

    @http.route(
        "/api/portal/warranty-statuses/<int:status_id>",
        type="http",
        auth="public",
        methods=["PUT"],
        csrf=False,
        cors="*",
    )
    def update_status(self, status_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        status_response = self._get_status(portal_user.crm_partner_id, status_id)
        if status_response["error"]:
            return status_response["error"]

        payload, parse_error = self._parse_payload()
        if parse_error:
            return parse_error

        vals = {}
        for field in ("code", "label", "sequence", "color", "active"):
            camel = self._to_camel(field)
            if field in payload or camel in payload:
                vals[field] = payload.get(field, payload.get(camel))

        if "is_default" in payload or "isDefault" in payload:
            vals["is_default"] = bool(payload.get("is_default") or payload.get("isDefault"))

        if "label" in vals and not (vals["label"] or "").strip():
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ label"},
                status=400,
            )
        if "code" in vals and not (vals["code"] or "").strip():
            return json_response(
                {"error": "invalid_request", "message": "กรุณาระบุ code"},
                status=400,
            )

        if not vals:
            return json_response(
                {"error": "invalid_request", "message": "ไม่มีข้อมูลสำหรับแก้ไข"},
                status=400,
            )

        try:
            status_response["status"].write(vals)
        except IntegrityError:
            request.env.cr.rollback()
            return json_response(
                {"error": "status_not_allowed", "message": "code นี้มีอยู่แล้ว"},
                status=400,
            )
        except ValidationError as error:
            request.env.cr.rollback()
            return json_response(
                {"error": "status_not_allowed", "message": str(error)},
                status=400,
            )

        return json_response({
            "status": self._serialize_status(status_response["status"]),
        })

    @http.route(
        "/api/portal/warranty-statuses/<int:status_id>",
        type="http",
        auth="public",
        methods=["DELETE"],
        csrf=False,
        cors="*",
    )
    def delete_status(self, status_id, **kwargs):
        portal_user, auth_error = get_authenticated_portal_user()
        if auth_error:
            return auth_error

        status_response = self._get_status(portal_user.crm_partner_id, status_id)
        if status_response["error"]:
            return status_response["error"]

        status_response["status"].write({"active": False})
        return json_response({"success": True})

    def _resolve_product_image_url(self, partner, payload, product=None):
        image_base64 = payload.get("image_base64")
        if image_base64 is not None:
            if not str(image_base64).strip():
                return False

            record = product or request.env["partner.warranty.product"].sudo().new({
                "partner_id": partner.id,
            })
            image_url = record._upload_image_field("image", image_base64)
            if not image_url:
                raise ValidationError("อัปโหลดรูปภาพไม่สำเร็จ")
            return image_url

        if "image" in payload or "image_url" in payload:
            image = payload.get("image", payload.get("image_url"))
            if image and not (
                str(image).startswith("http://") or str(image).startswith("https://")
            ):
                record = product or request.env["partner.warranty.product"].sudo().new({
                    "partner_id": partner.id,
                })
                image_url = record._upload_image_field("image", image)
                if not image_url:
                    raise ValidationError("อัปโหลดรูปภาพไม่สำเร็จ")
                return image_url
            return image or False

        return None

    def _product_vals_from_payload(self, payload, partner_id):
        name = (payload.get("name") or "").strip()
        if not name:
            return None, json_response(
                {"error": "invalid_request", "message": "กรุณาระบุชื่อสินค้า"},
                status=400,
            )

        return {
            "name": name,
            "description": payload.get("description") or False,
            "sku": payload.get("sku") or False,
            "cost_price": payload.get("cost_price") or payload.get("costPrice") or 0,
            "sell_price": payload.get("sell_price") or payload.get("sellPrice") or 0,
            "active": payload.get("active", True),
            "partner_id": partner_id,
        }, None

    def _search_products(self, partner, include_inactive=False):
        domain = [("partner_id", "=", partner.id)]
        if not include_inactive:
            domain.append(("active", "=", True))
        return request.env["partner.warranty.product"].sudo().search(
            domain,
            order="name asc, id asc",
        )

    def _search_contributors(self, partner, include_inactive=False):
        domain = [("partner_id", "=", partner.id)]
        if not include_inactive:
            domain.append(("active", "=", True))
        return request.env["partner.warranty.contributor"].sudo().search(
            domain,
            order="sequence asc, name asc, id asc",
        )

    def _search_statuses(self, partner, include_inactive=False):
        domain = [("partner_id", "=", partner.id)]
        if not include_inactive:
            domain.append(("active", "=", True))
        return request.env["partner.warranty.status"].sudo().search(
            domain,
            order="sequence asc, id asc",
        )

    def _get_warranty(self, partner, warranty_id):
        warranty = request.env["partner.warranty"].sudo().search([
            ("id", "=", warranty_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not warranty:
            return {
                "warranty": False,
                "error": json_response(
                    {"error": "warranty_not_found", "message": "ไม่พบรายการรับประกันดังกล่าว"},
                    status=404,
                ),
            }
        return {"warranty": warranty, "error": False}

    def _get_product(self, partner, product_id):
        product = request.env["partner.warranty.product"].sudo().search([
            ("id", "=", product_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not product:
            return {
                "product": False,
                "error": json_response(
                    {"error": "product_not_found", "message": "ไม่พบสินค้าดังกล่าว"},
                    status=404,
                ),
            }
        return {"product": product, "error": False}

    def _get_contributor(self, partner, contributor_id):
        contributor = request.env["partner.warranty.contributor"].sudo().search([
            ("id", "=", contributor_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not contributor:
            return {
                "contributor": False,
                "error": json_response(
                    {"error": "contributor_not_found", "message": "ไม่พบช่องทางการขายดังกล่าว"},
                    status=404,
                ),
            }
        return {"contributor": contributor, "error": False}

    def _get_status(self, partner, status_id):
        status = request.env["partner.warranty.status"].sudo().search([
            ("id", "=", status_id),
            ("partner_id", "=", partner.id),
        ], limit=1)
        if not status:
            return {
                "status": False,
                "error": json_response(
                    {"error": "status_not_found", "message": "ไม่พบสถานะดังกล่าว"},
                    status=404,
                ),
            }
        return {"status": status, "error": False}

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

    def _to_camel(self, value):
        parts = value.split("_")
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    def _serialize_warranty(self, warranty, include_comments=False):
        user = warranty.user_id
        data = {
            "id": warranty.id,
            "serial_number": warranty.serial_number,
            "receipt_number": warranty.receipt_number,
            "purchase_date": fields.Date.to_string(warranty.purchase_date),
            "receipt_image_url": warranty.receipt_image or False,
            "submitted_date": fields.Datetime.to_string(warranty.submitted_date),
            "product": self._serialize_product(warranty.product_id),
            "contributor": self._serialize_contributor(warranty.contributor_id),
            "status": self._serialize_status(warranty.status_id),
            "user": {
                "id": user.id,
                "display_name": user.display_name,
                "line_user_id": user.line_user_id,
                "email": user.email or False,
                "phone": user.phone or False,
                "picture_url": user.picture_url or False,
            },
        }
        if include_comments:
            data["comments"] = [
                self._serialize_comment(comment)
                for comment in warranty.comment_ids
            ]
        return data

    def _serialize_product(self, product):
        return {
            "id": product.id,
            "name": product.name,
            "description": product.description or False,
            "sku": product.sku or False,
            "cost_price": product.cost_price or 0,
            "sell_price": product.sell_price or 0,
            "image_url": product.image or False,
            "active": product.active,
        }

    def _serialize_contributor(self, contributor):
        return {
            "id": contributor.id,
            "name": contributor.name,
            "sequence": contributor.sequence,
            "active": contributor.active,
        }

    def _serialize_status(self, status):
        return {
            "id": status.id,
            "code": status.code,
            "label": status.label,
            "sequence": status.sequence,
            "color": status.color or False,
            "is_default": status.is_default,
            "active": status.active,
        }

    def _serialize_comment(self, comment):
        return {
            "id": comment.id,
            "body": comment.body,
            "author_name": comment.author_name or False,
            "author_id": comment.author_id.id if comment.author_id else False,
            "created_at": fields.Datetime.to_string(comment.create_date),
        }
