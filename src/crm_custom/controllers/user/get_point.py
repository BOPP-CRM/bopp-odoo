from odoo import fields, http
from odoo.http import request

from ....util.request import json_response


class GetPointController(http.Controller):
    @http.route("/api/partner/<string:slug>/user/<string:user_id>/point", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_point(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        user = user_response["user"]
        return json_response({
            "points": self._serialize_points(user),
        })

    @http.route("/api/partner/<string:slug>/user/<string:user_id>/point-history", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_point_history(self, slug, user_id, **kwargs):
        user_response = self._get_user(slug, user_id)
        if user_response["error"]:
            return user_response["error"]

        user = user_response["user"]
        return json_response({
            "point_history": self._serialize_point_history(user),
        })

    def _get_user(self, slug, user_id):
        partner = request.env["partner"].sudo().search(
            [
                ("slug", "=", slug),
            ],
            limit=1,
        )

        if not partner:
            return {
                "user": False,
                "error": json_response(
                    {
                        "error": "partner_not_found",
                        "message": "ไม่พบ Client โปรดติดต่อเจ้าหน้าที่",
                    },
                    status=404,
                ),
            }

        user = request.env["crm.user"].sudo().search(
            [
                ("line_user_id", "=", user_id),
                ("partner_id", "=", partner.id),
            ],
            limit=1,
        )

        if not user:
            return {
                "user": False,
                "error": json_response(
                    {
                        "error": "user_not_found",
                        "message": "ไม่พบผู้ใช้งานดังกล่าว",
                    },
                    status=404,
                ),
            }

        return {
            "user": user,
            "error": False,
        }

    def _serialize_points(self, user):
        currency_totals = {}

        for point in user.point_ids:
            currency = point.currency_id
            if currency.id not in currency_totals:
                currency_totals[currency.id] = {
                    "currency": {
                        "id": currency.id,
                        "name": currency.name,
                        "is_default": currency.is_default,
                    },
                    "earn": 0,
                    "transfer": 0,
                    "burn": 0,
                    "balance": 0,
                }

            currency_total = currency_totals[currency.id]
            currency_total[point.type] += point.value

        for currency_total in currency_totals.values():
            currency_total["balance"] = (
                currency_total["earn"]
                - currency_total["transfer"]
                - currency_total["burn"]
            )

        return list(currency_totals.values())

    def _serialize_point_history(self, user):
        points = user.point_ids.sorted(
            key=lambda point: point.given_date,
            reverse=True,
        )

        return [
            {
                "id": point.id,
                "name": point.name,
                "value": point.value,
                "type": point.type,
                "given_date": fields.Datetime.to_string(point.given_date),
                "expiration_date": fields.Datetime.to_string(point.expiration_date),
                "currency": {
                    "id": point.currency_id.id,
                    "name": point.currency_id.name,
                    "is_default": point.currency_id.is_default,
                    "is_total_spending": point.currency_id.is_total_spending,
                },
            }
            for point in points
        ]
