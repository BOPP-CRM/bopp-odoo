import json
import secrets

from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestPortalTeamHttp(HttpCase):
    """Portal team routes run as the public user and authorize with a bearer token."""

    def setUp(self):
        super().setUp()
        suffix = secrets.token_hex(4)
        self.partner = self.env["partner"].create({
            "name": "Team HTTP Tenant",
            "slug": f"team-http-{suffix}",
        })
        self.other_partner = self.env["partner"].create({
            "name": "Other Team Tenant",
            "slug": f"team-http-other-{suffix}",
        })
        Users = self.env["res.users"]
        self.admin = Users.create_partner_portal_user(
            self.partner,
            "Team Admin",
            f"team-admin-{suffix}@example.invalid",
            "team-admin-secret",
            portal_role="admin",
        )
        self.operation = Users.create_partner_portal_user(
            self.partner,
            "Team Operation",
            f"team-operation-{suffix}@example.invalid",
            "team-operation-secret",
            portal_role="operation",
        )
        Token = self.env["partner.portal.token"]
        self.admin_token = Token.create_for_user(self.admin).token
        self.operation_token = Token.create_for_user(self.operation).token
        self.portal_group = self.env.ref("crm_custom.group_partner_portal")
        self.internal_group = self.env.ref("base.group_user")

    def _post(self, path, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.url_open(
            path,
            data=json.dumps(payload).encode(),
            headers=headers,
        )

    def _portal_user(self, email):
        self.env.invalidate_all()
        return self.env["res.users"].search([
            ("login", "=", f"{self.partner.slug}:{email}"),
        ], limit=1)

    def test_admin_creates_team_user_with_portal_group(self):
        email = "new.member@example.invalid"
        response = self._post("/api/portal/team", {
            "name": "New Member",
            "email": email,
            "password": "member-secret",
            "role": "operation",
        }, token=self.admin_token)

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["team_user"]["email"], email)
        self.assertEqual(body["team_user"]["role"], "operation")
        self.assertTrue(body["team_user"]["active"])

        user = self._portal_user(email)
        self.assertTrue(user)
        self.assertEqual(user.crm_partner_id, self.partner)
        self.assertEqual(user.portal_role, "operation")
        self.assertIn(self.portal_group, user.groups_id)
        self.assertNotIn(self.internal_group, user.groups_id)

    def test_operation_user_cannot_create_team_user(self):
        email = "blocked.member@example.invalid"
        response = self._post("/api/portal/team", {
            "name": "Blocked Member",
            "email": email,
            "password": "member-secret",
            "role": "operation",
        }, token=self.operation_token)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"], "forbidden")
        self.assertFalse(self._portal_user(email))

    def test_admin_creates_invite_for_own_partner(self):
        email = "invited.member@example.invalid"
        response = self._post("/api/portal/team/invites", {
            "name": "Invited Member",
            "email": email,
            "role": "operation",
        }, token=self.admin_token)

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()["invite"]
        self.assertEqual(body["email"], email)
        self.assertEqual(body["role"], "operation")
        self.assertEqual(body["state"], "pending")
        self.assertTrue(body["token"])

        self.env.invalidate_all()
        invite = self.env["partner.portal.invite"].search([
            ("token", "=", body["token"]),
        ], limit=1)
        self.assertEqual(invite.partner_id, self.partner)
        self.assertEqual(invite.invited_by_id, self.admin)
        self.assertNotEqual(invite.partner_id, self.other_partner)

    def test_operation_user_cannot_create_invite(self):
        response = self._post("/api/portal/team/invites", {
            "name": "Blocked Invite",
            "email": "blocked.invite@example.invalid",
            "role": "admin",
        }, token=self.operation_token)

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"], "forbidden")
        self.env.invalidate_all()
        self.assertFalse(self.env["partner.portal.invite"].search([
            ("email", "=", "blocked.invite@example.invalid"),
            ("partner_id", "=", self.partner.id),
        ]))

    def test_accept_invite_creates_portal_user(self):
        email = "accepted.member@example.invalid"
        invite = self.env["partner.portal.invite"].create_invite(
            self.partner,
            self.admin,
            "Accepted Member",
            email,
            portal_role="operation",
        )

        response = self._post(f"/api/portal/invite/{invite.token}/accept", {
            "name": "Accepted Member",
            "password": "accepted-secret",
        })

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["email"], email)
        self.assertEqual(body["user"]["role"], "operation")
        self.assertEqual(body["partner"]["slug"], self.partner.slug)

        user = self._portal_user(email)
        self.assertTrue(user)
        self.assertEqual(user.crm_partner_id, self.partner)
        self.assertEqual(user.portal_role, "operation")
        self.assertIn(self.portal_group, user.groups_id)
        self.assertNotIn(self.internal_group, user.groups_id)
        invite.invalidate_recordset()
        self.assertEqual(invite.state, "accepted")
        self.assertEqual(invite.accepted_user_id, user)
