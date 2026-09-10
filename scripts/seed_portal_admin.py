"""Seed a portal admin user for local testing and print a ready-to-use bearer token.

Usage (from repo root):
    docker compose exec -T odoo odoo shell -c /etc/odoo/odoo.conf -d crm_backend --no-http \
        < scripts/seed_portal_admin.py

Override defaults with env vars: PARTNER_SLUG, PORTAL_EMAIL, PORTAL_PASSWORD.
"""
import os

partner_slug = os.environ.get("PARTNER_SLUG")
email = os.environ.get("PORTAL_EMAIL", "portal-admin@example.com").strip().lower()
password = os.environ.get("PORTAL_PASSWORD", "portal1234")

Partner = env["partner"].sudo()
partner = (
    Partner.search([("slug", "=", partner_slug)], limit=1)
    if partner_slug
    else Partner.search([], limit=1)
)
if not partner:
    raise SystemExit("No partner found - create one first.")

Users = env["res.users"].sudo()
login = Users._make_portal_login(partner, email)
user = Users.with_context(active_test=False).search([("login", "=", login)], limit=1)
if user:
    user.write({"portal_role": "admin", "password": password, "active": True})
else:
    user = Users.create_partner_portal_user(
        partner, "Portal Admin (test)", email, password, portal_role="admin"
    )

token = env["partner.portal.token"].sudo().create_for_user(user)
env.cr.commit()

print("\n=== portal admin ready ===")
print("partner slug :", partner.slug)
print("login        :", login)
print("email        :", email)
print("password     :", password)
print("bearer token :", token.token)
print("expires at   :", token.expires_at)
print("\ncurl example:")
print(
    f'  curl -s http://localhost:8069/api/portal/appearance '
    f'-H "Authorization: Bearer {token.token}"'
)
