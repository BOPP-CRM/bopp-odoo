import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import odoo.http
from odoo.tests.common import TransactionCase, tagged

from ..controllers.portal.receipts import PortalReceiptsController
from ..models.mixins.s3_image import S3ImageMixin

RECEIPT_COUNT = 105
IMAGE_URL = "https://example.invalid/receipts/{index}.jpg"


@tagged("post_install", "-at_install")
class TestPortalReceiptList(TransactionCase):
    def setUp(self):
        super().setUp()
        suffix = secrets.token_hex(4)
        self.partner = self.env["partner"].create({
            "name": "Receipt List Tenant",
            "slug": f"receipt-list-{suffix}",
        })
        self.tier = self.partner.tier_ids[:1]
        self.assertTrue(self.tier)
        self.assertEqual(self.tier.convert_points, 25)
        self.member = self.env["crm.user"].create({
            "display_name": "Receipt Member",
            "picture_url": "https://example.invalid/pic.png",
            "line_user_id": f"Ureceipt{suffix}",
            "partner_id": self.partner.id,
            "tier_id": self.tier.id,
        })
        self.portal_user = self.env["res.users"].create_partner_portal_user(
            self.partner,
            "Receipt Admin",
            f"receipt-admin-{suffix}@example.invalid",
            "receipt-admin-secret",
        )
        self.token = self.env["partner.portal.token"].create_for_user(self.portal_user).token
        self.controller = PortalReceiptsController()
        base = datetime(2026, 1, 1)
        self.env["crm.partner.receipt.redeem"].with_context(
            tracking_disable=True,
            mail_create_nosubscribe=True,
        ).create([
            {
                "receipt_number": f"R{index:04d}",
                "receipt_image": IMAGE_URL.format(index=index),
                "amount": 100 if index == RECEIPT_COUNT - 1 else 1,
                "state": "pending" if index == RECEIPT_COUNT - 1 else "approved",
                "tier_convert_points": 0,
                "reward_points": 0,
                "partner_id": self.partner.id,
                "user_id": self.member.id,
                "submitted_date": base + timedelta(seconds=index),
            }
            for index in range(RECEIPT_COUNT)
        ])

    @contextmanager
    def _portal_request(self):
        req = SimpleNamespace(
            env=self.env,
            httprequest=SimpleNamespace(
                headers={"Authorization": f"Bearer {self.token}"},
                path="/api/portal/receipts",
            ),
        )
        odoo.http._request_stack.push(req)
        try:
            yield
        finally:
            popped = odoo.http._request_stack.pop()
            if popped is not req:
                raise AssertionError("request stack was not restored")

    def _list(self, **kwargs):
        with self._portal_request():
            response = self.controller.list_receipts(**kwargs)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.get_data(as_text=True))
        self.assertEqual(list(payload.keys()), ["receipts", "total"])
        return payload

    def test_list_skips_s3_fetch_and_applies_default_and_cap(self):
        with patch.object(S3ImageMixin, "_fetch_image_from_url", return_value=b"image-bytes") as fetch:
            missing = self._list()
            invalid = self._list(limit="nope")
            zero = self._list(limit="0")
            negative = self._list(limit="-3")
            explicit = self._list(limit="50")
            at_cap = self._list(limit="100")
            over_cap = self._list(limit="1000")
            second_page = self._list(offset="20")
            with self._portal_request():
                detail = self.controller.get_receipt(missing["receipts"][0]["id"])

        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(detail.status_code, 200)

        for payload in (missing, invalid, zero, negative):
            self.assertEqual(payload["total"], RECEIPT_COUNT)
            self.assertEqual(len(payload["receipts"]), 20)
            self.assertEqual(payload["receipts"][0]["receipt_number"], "R0104")
            self.assertEqual(payload["receipts"][-1]["receipt_number"], "R0085")

        self.assertEqual(len(explicit["receipts"]), 50)
        self.assertEqual(explicit["receipts"][0]["receipt_number"], "R0104")
        self.assertEqual(explicit["receipts"][-1]["receipt_number"], "R0055")
        self.assertEqual(explicit["total"], RECEIPT_COUNT)

        self.assertEqual(len(at_cap["receipts"]), 100)
        self.assertEqual(len(over_cap["receipts"]), 100)
        self.assertEqual(over_cap["receipts"][0]["receipt_number"], "R0104")
        self.assertEqual(over_cap["receipts"][-1]["receipt_number"], "R0005")
        self.assertEqual(over_cap["total"], RECEIPT_COUNT)

        self.assertEqual(len(second_page["receipts"]), 20)
        self.assertEqual(second_page["receipts"][0]["receipt_number"], "R0084")
        self.assertEqual(second_page["receipts"][-1]["receipt_number"], "R0065")
        self.assertEqual(second_page["total"], RECEIPT_COUNT)

        newest = missing["receipts"][0]
        self.assertEqual(
            list(newest.keys()),
            [
                "id",
                "receipt_number",
                "receipt_image_url",
                "amount",
                "state",
                "reject_reason",
                "submitted_date",
                "reviewed_date",
                "reviewed_by",
                "tier_convert_points",
                "reward_points",
                "earn_channel",
                "tier",
                "user",
                "spending_point",
                "reward_point",
            ],
        )
        self.assertEqual(newest["receipt_image_url"], IMAGE_URL.format(index=RECEIPT_COUNT - 1))
        self.assertNotIn("receipt_image_file", newest)
        self.assertEqual(newest["state"], "pending")
        self.assertEqual(newest["amount"], 100)
        self.assertEqual(newest["tier_convert_points"], 25)
        self.assertEqual(newest["reward_points"], 4)
        self.assertEqual(newest["earn_channel"], "receipt")
        self.assertEqual(
            list(newest["user"].keys()),
            ["id", "display_name", "line_user_id", "email", "phone", "picture_url"],
        )
        self.assertEqual(
            list(newest["tier"].keys()),
            ["id", "code", "name", "convert_points"],
        )
        self.assertFalse(newest["spending_point"])
        self.assertFalse(newest["reward_point"])

        detail_payload = json.loads(detail.get_data(as_text=True))
        self.assertEqual(list(detail_payload.keys()), ["receipt"])
        self.assertEqual(detail_payload["receipt"]["id"], newest["id"])
        self.assertEqual(
            detail_payload["receipt"]["receipt_image_url"],
            newest["receipt_image_url"],
        )
