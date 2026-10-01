import secrets
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


def _item(status, price, order_id="1001", sub_order_id="s1"):
    return {
        "order_id": order_id,
        "order_item_id": sub_order_id,
        "status": status,
        "paid_price": price,
    }


@tagged("post_install", "-at_install")
class TestLazadaClaim(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["partner"].create({
            "name": "Test Lazada Tenant",
            "slug": f"test-lazada-tenant-{secrets.token_hex(4)}",
        })
        self.partner.write({
            "lazada_enabled": True,
            "lazada_access_token": "fake-token",
        })
        self.user = self.env["crm.user"].create({
            "display_name": "Lazada Buyer",
            "picture_url": "https://example.com/pic.png",
            "line_user_id": "Uxxxxlazada",
            "partner_id": self.partner.id,
        })
        self.claim_model = self.env["partner.lazada.order.claim"]

    def _patch_items(self, items):
        return patch.object(
            type(self.partner),
            "fetch_lazada_order_items",
            lambda self_partner, order_id: items,
        )

    def test_all_items_delivered_awards_full_amount(self):
        items = [_item("delivered", 100, order_id="1"), _item("delivered", 50, order_id="1")]
        with self._patch_items(items):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "1")

        self.assertEqual(claim.amount, 150)
        self.assertEqual(claim.qualifying_item_count, 2)
        self.assertEqual(claim.total_item_count, 2)
        self.assertFalse(claim.used_flat_fallback)
        self.assertTrue(claim.spending_point_id)

    def test_partial_delivery_awards_only_delivered_items(self):
        items = [
            _item("delivered", 100, order_id="2"),
            _item("unpaid", 999, order_id="2"),
            _item("canceled", 999, order_id="2"),
        ]
        with self._patch_items(items):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "2")

        self.assertEqual(claim.amount, 100)
        self.assertEqual(claim.qualifying_item_count, 1)
        self.assertEqual(claim.total_item_count, 3)

    def test_no_delivered_items_raises_and_does_not_persist(self):
        items = [_item("ready_to_ship", 100, order_id="3")]
        with self._patch_items(items):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "3")

        self.assertFalse(self.claim_model.search([
            ("partner_id", "=", self.partner.id), ("order_number", "=", "3"),
        ]))

    def test_order_not_found_raises(self):
        with self._patch_items([]):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "nope")

    def test_duplicate_claim_is_rejected_without_calling_api(self):
        items = [_item("delivered", 100, order_id="4")]
        with self._patch_items(items):
            self.claim_model.verify_order_for_points(self.partner, self.user, "4")

        with patch.object(type(self.partner), "fetch_lazada_order_items") as mocked:
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "4")
            mocked.assert_not_called()

    def test_delivered_item_with_no_price_uses_flat_fallback(self):
        self.partner.lazada_flat_point_value = 10
        items = [_item("delivered", 0, order_id="5")]
        with self._patch_items(items):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "5")

        self.assertTrue(claim.used_flat_fallback)
        self.assertEqual(claim.reward_point_id.value, 10)
        self.assertFalse(claim.spending_point_id)

    def test_delivered_item_with_no_price_and_no_fallback_configured_raises(self):
        self.partner.lazada_flat_point_value = 0
        items = [_item("delivered", 0, order_id="6")]
        with self._patch_items(items):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "6")
