from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


def _order_payload(order_number, order_id, items):
    return {
        "order_number": order_number,
        "order_id": order_id,
        "member_sub_order_list": items,
    }


def _item(status, price, sub_order_id="sub-1"):
    return {"sub_order_id": sub_order_id, "status": status, "paid_price": price}


@tagged("post_install", "-at_install")
class TestLazadaClaim(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["partner"].create({
            "name": "Test Lazada Tenant",
            "slug": "test-lazada-tenant",
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

    def _patch_transactions(self, orders):
        return patch.object(
            type(self.partner),
            "fetch_lazada_transactions",
            lambda self_partner, **kwargs: orders,
        )

    def test_all_items_delivered_awards_full_amount(self):
        orders = [_order_payload("ORD-1", "1001", [
            _item("delivered", 100, "s1"),
            _item("delivered", 50, "s2"),
        ])]
        with self._patch_transactions(orders):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-1")

        self.assertEqual(claim.amount, 150)
        self.assertEqual(claim.qualifying_item_count, 2)
        self.assertEqual(claim.total_item_count, 2)
        self.assertFalse(claim.used_flat_fallback)
        self.assertTrue(claim.spending_point_id)

    def test_partial_delivery_awards_only_delivered_items(self):
        orders = [_order_payload("ORD-2", "1002", [
            _item("delivered", 100, "s1"),
            _item("unpaid", 999, "s2"),
            _item("canceled", 999, "s3"),
        ])]
        with self._patch_transactions(orders):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-2")

        self.assertEqual(claim.amount, 100)
        self.assertEqual(claim.qualifying_item_count, 1)
        self.assertEqual(claim.total_item_count, 3)

    def test_no_delivered_items_raises_and_does_not_persist(self):
        orders = [_order_payload("ORD-3", "1003", [
            _item("unpaid", 100, "s1"),
        ])]
        with self._patch_transactions(orders):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-3")

        self.assertFalse(self.claim_model.search([
            ("partner_id", "=", self.partner.id), ("order_number", "=", "ORD-3"),
        ]))

    def test_order_not_found_raises(self):
        with self._patch_transactions([]):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "NOPE")

    def test_duplicate_claim_is_rejected_without_calling_api(self):
        orders = [_order_payload("ORD-4", "1004", [_item("delivered", 100, "s1")])]
        with self._patch_transactions(orders):
            self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-4")

        with patch.object(type(self.partner), "fetch_lazada_transactions") as mocked:
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-4")
            mocked.assert_not_called()

    def test_delivered_item_with_no_price_uses_flat_fallback(self):
        self.partner.lazada_flat_point_value = 10
        orders = [_order_payload("ORD-5", "1005", [_item("delivered", 0, "s1")])]
        with self._patch_transactions(orders):
            claim = self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-5")

        self.assertTrue(claim.used_flat_fallback)
        self.assertEqual(claim.reward_point_id.value, 10)
        self.assertFalse(claim.spending_point_id)

    def test_delivered_item_with_no_price_and_no_fallback_configured_raises(self):
        self.partner.lazada_flat_point_value = 0
        orders = [_order_payload("ORD-6", "1006", [_item("delivered", 0, "s1")])]
        with self._patch_transactions(orders):
            with self.assertRaises(ValidationError):
                self.claim_model.verify_order_for_points(self.partner, self.user, "ORD-6")
