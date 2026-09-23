import unittest

from ...util.lazada_signature import build_lazada_signature


class TestLazadaSignature(unittest.TestCase):
    def _base_params(self):
        return {
            "app_key": "142409",
            "timestamp": "1700000000000",
            "sign_method": "sha256",
            "access_token": "test-access-token",
        }

    def test_deterministic(self):
        params = self._base_params()
        sign1 = build_lazada_signature("/partner/transaction", params, "s3cr3t")
        sign2 = build_lazada_signature("/partner/transaction", dict(params), "s3cr3t")
        self.assertEqual(sign1, sign2)

    def test_output_shape(self):
        sign = build_lazada_signature("/partner/transaction", self._base_params(), "s3cr3t")
        self.assertEqual(len(sign), 64)
        self.assertEqual(sign, sign.upper())
        int(sign, 16)  # raises ValueError if not valid hex

    def test_key_order_does_not_matter(self):
        params = self._base_params()
        reordered = dict(reversed(list(params.items())))
        sign1 = build_lazada_signature("/partner/transaction", params, "s3cr3t")
        sign2 = build_lazada_signature("/partner/transaction", reordered, "s3cr3t")
        self.assertEqual(sign1, sign2)

    def test_changing_any_param_changes_signature(self):
        base_sign = build_lazada_signature("/partner/transaction", self._base_params(), "s3cr3t")

        changed_value = self._base_params()
        changed_value["timestamp"] = "1700000000001"
        self.assertNotEqual(base_sign, build_lazada_signature("/partner/transaction", changed_value, "s3cr3t"))

        changed_path = build_lazada_signature("/partner/transaction/other", self._base_params(), "s3cr3t")
        self.assertNotEqual(base_sign, changed_path)

        changed_secret = build_lazada_signature("/partner/transaction", self._base_params(), "different-secret")
        self.assertNotEqual(base_sign, changed_secret)

    def test_sign_key_itself_is_excluded_from_the_base_string(self):
        params = self._base_params()
        without_sign = build_lazada_signature("/partner/transaction", params, "s3cr3t")

        params_with_sign = dict(params)
        params_with_sign["sign"] = "whatever-was-here-before"
        with_sign = build_lazada_signature("/partner/transaction", params_with_sign, "s3cr3t")

        self.assertEqual(without_sign, with_sign)


if __name__ == "__main__":
    unittest.main()
