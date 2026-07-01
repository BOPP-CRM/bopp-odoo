from collections import defaultdict
from datetime import datetime, timedelta, time
import re

from odoo import fields, http
from odoo.http import request

from ....util.portal_auth import get_portal_admin_from_request
from ....util.request import json_response

GRANULARITIES = {
    "day": "day",
    "week": "week",
    "month": "month",
}
THAILAND_TZ = "Asia/Bangkok"
THAILAND_OFFSET = timedelta(hours=7)
ODOO_WEEK_PATTERN = re.compile(r"^W(\d+)\s+(\d{4})$")


class PortalDashboardController(http.Controller):
    @http.route("/api/portal/dashboard", type="http", auth="public", methods=["GET"], csrf=False, cors="*")
    def get_dashboard(self, **kwargs):
        user, auth_error = get_portal_admin_from_request()
        if auth_error:
            return auth_error

        partner = user.crm_partner_id
        date_range, error = self._parse_date_range(kwargs)
        if error:
            return error

        granularity = self._parse_granularity(kwargs.get("granularity"))
        date_from = date_range["date_from"]
        date_to = date_range["date_to"]

        return json_response({
            "date_from": fields.Datetime.to_string(self._to_thailand_time(date_from)),
            "date_to": fields.Datetime.to_string(self._to_thailand_time(date_to)),
            "granularity": granularity,
            "members_by_tier": self._get_members_by_tier(partner),
            "user_registrations": self._get_user_registrations(partner, date_from, date_to, granularity),
            "user_registrations_by_hour": self._get_user_registrations_by_hour(partner, date_from, date_to),
            "receipt_amounts": self._get_receipt_amounts(partner, date_from, date_to, granularity),
            "coupons_by_name": self._get_coupons_by_name(partner, date_from, date_to),
            "points": self._get_points_summary(partner, date_from, date_to, granularity),
        })

    def _normalize_datetime(self, value):
        if not value:
            return None

        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value

        if isinstance(value, str):
            return fields.Datetime.from_string(value)

        if hasattr(value, "year") and hasattr(value, "day"):
            if hasattr(value, "hour"):
                return value
            return datetime.combine(value, time.min)

        return None

    def _to_thailand_time(self, value):
        utc_dt = self._normalize_datetime(value)
        if not utc_dt:
            return None

        return utc_dt + THAILAND_OFFSET

    def _parse_date_range(self, kwargs):
        now = fields.Datetime.now()
        default_from = now - timedelta(days=30)

        date_from = self._parse_query_datetime(kwargs.get("date_from"), end_of_day=False) or default_from
        date_to = self._parse_query_datetime(kwargs.get("date_to"), end_of_day=True) or now

        if date_from > date_to:
            return None, json_response(
                {
                    "error": "invalid_date_range",
                    "message": "date_from must be earlier than or equal to date_to.",
                },
                status=400,
            )

        return {"date_from": date_from, "date_to": date_to}, None

    def _parse_query_datetime(self, value, end_of_day=False):
        if not value:
            return None

        text = str(value).strip()
        if not text:
            return None

        parsed = fields.Datetime.to_datetime(text)
        if len(text) <= 10:
            local_dt = datetime.combine(
                parsed.date(),
                time(23, 59, 59) if end_of_day else time.min,
            )
        else:
            local_dt = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

        return local_dt - THAILAND_OFFSET

    def _parse_granularity(self, value):
        granularity = (value or "day").strip().lower()
        return GRANULARITIES.get(granularity, "day")

    def _get_members_by_tier(self, partner):
        user_model = request.env["crm.user"].sudo()
        tier_model = request.env["partner.tier"].sudo()

        groups = user_model.read_group(
            [("partner_id", "=", partner.id), ("active", "=", True)],
            ["__count"],
            ["tier_id"],
            lazy=False,
        )
        counts_by_tier_id = {}
        unassigned_count = 0
        for group in groups:
            if group.get("tier_id"):
                counts_by_tier_id[group["tier_id"][0]] = group["__count"]
            else:
                unassigned_count = group["__count"]

        tiers = tier_model.search([("partner_id", "=", partner.id)], order="min_spending asc, id asc")
        results = []
        for tier in tiers:
            results.append({
                "tier_id": tier.id,
                "tier_code": tier.code,
                "tier_name": tier.name,
                "count": counts_by_tier_id.get(tier.id, 0),
            })

        if unassigned_count:
            results.append({
                "tier_id": False,
                "tier_code": False,
                "tier_name": "Unassigned",
                "count": unassigned_count,
            })

        return results

    def _search_users_in_range(self, partner, date_from, date_to):
        return request.env["crm.user"].sudo().search([
            ("partner_id", "=", partner.id),
            ("create_date", ">=", date_from),
            ("create_date", "<=", date_to),
        ])

    def _get_user_registrations(self, partner, date_from, date_to, granularity):
        counts = defaultdict(int)
        for user in self._search_users_in_range(partner, date_from, date_to):
            thailand_time = self._to_thailand_time(user.create_date)
            if not thailand_time:
                continue
            period = self._period_key_from_thailand_time(thailand_time, granularity)
            counts[period] += 1

        return self._build_time_series(counts, date_from, date_to, granularity, "count", default=0)

    def _get_user_registrations_by_hour(self, partner, date_from, date_to):
        hour_counts = {hour: 0 for hour in range(24)}
        for user in self._search_users_in_range(partner, date_from, date_to):
            thailand_time = self._to_thailand_time(user.create_date)
            if not thailand_time:
                continue
            hour_counts[thailand_time.hour] += 1

        return [
            {"hour": hour, "count": hour_counts[hour]}
            for hour in range(24)
        ]

    def _get_receipt_amounts(self, partner, date_from, date_to, granularity):
        receipt_model = request.env["crm.partner.receipt.redeem"].sudo()
        receipts = receipt_model.search([
            ("partner_id", "=", partner.id),
            ("state", "=", "approved"),
            ("reviewed_date", ">=", date_from),
            ("reviewed_date", "<=", date_to),
        ])

        amounts = defaultdict(float)
        counts = defaultdict(int)
        for receipt in receipts:
            thailand_time = self._to_thailand_time(receipt.reviewed_date)
            if not thailand_time:
                continue
            period = self._period_key_from_thailand_time(thailand_time, granularity)
            amounts[period] += receipt.amount or 0.0
            counts[period] += 1

        results = []
        for period in self._iter_periods(date_from, date_to, granularity):
            results.append({
                "period": period,
                "amount": amounts.get(period, 0.0),
                "count": counts.get(period, 0),
            })
        return results

    def _get_coupons_by_name(self, partner, date_from, date_to):
        coupon_model = request.env["crm.user.coupon"].sudo()
        user_coupons = coupon_model.search([
            ("partner_id", "=", partner.id),
            ("acquired_date", ">=", date_from),
            ("acquired_date", "<=", date_to),
        ])

        stats = defaultdict(lambda: {
            "coupon_id": False,
            "coupon_name": "",
            "redeemed_count": 0,
            "used_count": 0,
            "expired_count": 0,
        })

        for user_coupon in user_coupons:
            coupon = user_coupon.coupon_id
            key = coupon.id
            entry = stats[key]
            entry["coupon_id"] = coupon.id
            entry["coupon_name"] = coupon.name

            if user_coupon.is_used or user_coupon.state == "used":
                entry["used_count"] += 1
            elif user_coupon.state == "expired":
                entry["expired_count"] += 1
            else:
                entry["redeemed_count"] += 1

        return sorted(
            [
                {
                    **item,
                    "total_count": item["redeemed_count"] + item["used_count"] + item["expired_count"],
                }
                for item in stats.values()
            ],
            key=lambda item: (-item["total_count"], item["coupon_name"]),
        )

    def _get_points_summary(self, partner, date_from, date_to, granularity):
        point_model = request.env["crm.user.point"].sudo()
        point_currencies = partner.currency_ids.filtered(lambda currency: not currency.is_total_spending)
        domain = [
            ("user_id.partner_id", "=", partner.id),
            ("given_date", ">=", date_from),
            ("given_date", "<=", date_to),
        ]
        if point_currencies:
            domain.append(("currency_id", "in", point_currencies.ids))

        points = point_model.search(domain)
        earned = defaultdict(float)
        used = defaultdict(float)
        for point in points:
            thailand_time = self._to_thailand_time(point.given_date)
            if not thailand_time:
                continue
            period = self._period_key_from_thailand_time(thailand_time, granularity)
            if point.type == "earn":
                earned[period] += point.value or 0.0
            elif point.type == "burn":
                used[period] += point.value or 0.0

        default_currency = partner.currency_ids.filtered("is_default")[:1]
        series = []
        for period in self._iter_periods(date_from, date_to, granularity):
            series.append({
                "period": period,
                "earned": earned.get(period, 0.0),
                "used": used.get(period, 0.0),
            })

        return {
            "currency": {
                "id": default_currency.id,
                "name": default_currency.name,
            } if default_currency else False,
            "series": series,
        }

    def _build_time_series(self, values_by_period, date_from, date_to, granularity, value_key, default=0):
        return [
            {
                "period": period,
                value_key: values_by_period.get(period, default),
            }
            for period in self._iter_periods(date_from, date_to, granularity)
        ]

    def _period_key_from_thailand_time(self, thailand_time, granularity):
        if granularity == "month":
            return thailand_time.strftime("%Y-%m")
        if granularity == "week":
            return self._format_week_period(thailand_time.date())
        return fields.Date.to_string(thailand_time.date())

    def _format_week_period(self, value):
        if isinstance(value, str):
            text = value.strip()
            odoo_match = ODOO_WEEK_PATTERN.match(text)
            if odoo_match:
                week_num, year = odoo_match.groups()
                return f"{year}-W{int(week_num):02d}"

            if "-W" in text:
                year_part, week_part = text.split("-W", 1)
                return f"{year_part}-W{int(week_part):02d}"

            parsed = fields.Date.to_date(text)
        elif isinstance(value, datetime):
            parsed = value.date()
        elif hasattr(value, "year") and hasattr(value, "month"):
            parsed = value
        else:
            return str(value)

        iso_year, iso_week, _ = parsed.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    def _iter_periods(self, date_from, date_to, granularity):
        th_from = self._to_thailand_time(date_from)
        th_to = self._to_thailand_time(date_to)

        if granularity == "month":
            current = th_from.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = th_to.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            periods = []
            while current <= end:
                periods.append(current.strftime("%Y-%m"))
                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1)
                else:
                    current = current.replace(month=current.month + 1)
            return periods

        if granularity == "week":
            current = th_from.date()
            end = th_to.date()
            current -= timedelta(days=current.weekday())
            periods = []
            seen = set()
            while current <= end:
                period = self._format_week_period(current)
                if period not in seen:
                    seen.add(period)
                    periods.append(period)
                current += timedelta(days=7)
            return periods

        current_date = th_from.date()
        end_date = th_to.date()
        periods = []
        while current_date <= end_date:
            periods.append(fields.Date.to_string(current_date))
            current_date += timedelta(days=1)
        return periods
