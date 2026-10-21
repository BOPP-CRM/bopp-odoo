import secrets

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCouponDistributionRule(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env = self.env(context=dict(self.env.context, broadcast_no_commit=True))
        self.partner = self.env["partner"].create({
            "name": "Test Rule Tenant",
            "slug": f"test-rule-tenant-{secrets.token_hex(4)}",
        })
        # A default base tier is created with the partner; cap it to make room for Gold.
        self.base_tier = self.env["partner.tier"].search([("partner_id", "=", self.partner.id)], limit=1)
        self.base_tier.max_spending = 999
        self.gold_tier = self.env["partner.tier"].create({
            "name": "Gold",
            "code": "gold",
            "partner_id": self.partner.id,
            "min_spending": 1000,
            "max_spending": 1e9,
        })
        self.coupon = self._coupon(self.partner, "Cosmetics 10%")
        self.coupon.create_generated_codes(20)
        self.female_gold = self._user("F", self.gold_tier)
        self.female_base = self._user("F", self.base_tier)
        self.male_gold = self._user("M", self.gold_tier)

    def _coupon(self, partner, name, prefix="T"):
        currency = self.env["crm.partner.currency"].search(
            [("partner_id", "=", partner.id), ("is_default", "=", True)], limit=1
        ) or self.env["crm.partner.currency"].search([("partner_id", "=", partner.id)], limit=1)
        return self.env["partner.coupon"].create({
            "name": name,
            "partner_id": partner.id,
            "currency_id": currency.id,
            "value": 10,
            "prefix_code": prefix,
        })

    def _user(self, gender, tier):
        suffix = secrets.token_hex(4)
        user = self.env["crm.user"].create({
            "display_name": f"User {suffix}",
            "picture_url": "https://example.com/pic.png",
            "line_user_id": f"U{suffix}",
            "partner_id": self.partner.id,
            "gender": gender,
        })
        user.tier_id = tier
        return user

    def _rule(self, **vals):
        base = {
            "name": "Rule",
            "partner_id": self.partner.id,
            "coupon_id": self.coupon.id,
        }
        base.update(vals)
        return self.env["partner.coupon.distribution.rule"].create(base)

    def _holders(self, rule):
        return self.env["crm.user.coupon"].search([("distribution_rule_id", "=", rule.id)]).mapped("user_id")

    def test_gender_rule_grants_only_matching_gender(self):
        rule = self._rule(seg_gender="F")
        rule.action_run()
        self.assertEqual(self._holders(rule), self.female_gold | self.female_base)

    def test_tier_rule_grants_only_matching_tier(self):
        rule = self._rule(seg_tier_ids=[(6, 0, self.gold_tier.ids)])
        rule.action_run()
        self.assertEqual(self._holders(rule), self.female_gold | self.male_gold)

    def test_combined_conditions(self):
        rule = self._rule(seg_gender="F", seg_tier_ids=[(6, 0, self.gold_tier.ids)])
        rule.action_run()
        self.assertEqual(self._holders(rule), self.female_gold)

    def test_rerun_is_idempotent_and_picks_up_new_members(self):
        rule = self._rule(seg_gender="F", trigger="scheduled")
        rule.action_run()
        self.assertEqual(rule.granted_count, 2)
        self.assertFalse(rule._run("rule_auto"))
        self.assertEqual(rule.granted_count, 2)

        newcomer = self._user("F", self.base_tier)
        rule._run("rule_auto")
        rule.invalidate_recordset(["granted_count"])
        self.assertEqual(rule.granted_count, 3)
        self.assertIn(newcomer, self._holders(rule))

    def test_pool_shortage_fails_preflight(self):
        small = self._coupon(self.partner, "Small", prefix="S")
        small.create_generated_codes(1)
        rule = self._rule(coupon_id=small.id, seg_gender="F")
        with self.assertRaises(ValidationError):
            rule._run("rule_manual")

    def test_coupon_only_run_does_not_need_line(self):
        rule = self._rule(seg_gender="M", notify_line=False)
        self.assertFalse(self.partner.line_messaging_enabled)
        broadcast = rule._run("rule_manual")
        self.assertEqual(broadcast.state, "done")
        self.assertEqual(rule.granted_count, 1)

    def test_coupon_must_belong_to_partner(self):
        other = self.env["partner"].create({
            "name": "Other",
            "slug": f"other-{secrets.token_hex(4)}",
        })
        other_coupon = self._coupon(other, "Other", prefix="O")
        with self.assertRaises(ValidationError):
            self._rule(coupon_id=other_coupon.id)

    def test_scheduled_cron_runs_due_rules_only(self):
        due = self._rule(seg_gender="F", trigger="scheduled")
        manual = self._rule(seg_gender="M", trigger="manual")
        self.env["partner.coupon.distribution.rule"]._cron_run_scheduled_rules()
        self.assertEqual(due.granted_count, 2)
        self.assertEqual(manual.granted_count, 0)
