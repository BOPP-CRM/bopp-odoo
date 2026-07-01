from odoo.http import request

from .request import json_response

PORTAL_ROLE_ADMIN = "admin"
PORTAL_ROLE_OPERATION = "operation"
PORTAL_ROLES = {PORTAL_ROLE_ADMIN, PORTAL_ROLE_OPERATION}
PORTAL_API_KEY_PATH_PREFIX = "/api/portal/api-key"


def get_bearer_token():
    authorization = request.httprequest.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return False


def get_api_key_from_request():
    return (request.httprequest.headers.get("X-api-key") or "").strip() or False


def _get_portal_auth_error():
    return getattr(request, "portal_auth_error", None)


def _should_track_api_key_usage():
    path = request.httprequest.path or ""
    return not path.startswith(PORTAL_API_KEY_PATH_PREFIX)


def get_portal_user_from_request():
    token = get_bearer_token()
    if token:
        # Bearer token (portal login) does not count toward API key monthly usage.
        return request.env["partner.portal.token"].sudo().get_user_from_token(token)

    api_key = get_api_key_from_request()
    if api_key:
        user = request.env["res.users"].sudo().get_user_from_api_key(api_key)
        # Only X-api-key requests consume the partner monthly quota.
        if user and _should_track_api_key_usage():
            allowed, usage = request.env["partner.portal.api.usage"].sudo().try_consume(
                user.crm_partner_id
            )
            if not allowed:
                request.portal_auth_error = portal_rate_limit_response(usage)
                return request.env["res.users"]
        return user

    return request.env["res.users"]


def get_portal_role(user):
    if not user:
        return False
    return user.portal_role or PORTAL_ROLE_ADMIN


def is_portal_admin(user):
    return get_portal_role(user) == PORTAL_ROLE_ADMIN


def portal_unauthorized_response():
    return json_response(
        {"error": "unauthorized", "message": "Invalid or expired token."},
        status=401,
    )


def portal_forbidden_response():
    return json_response(
        {"error": "forbidden", "message": "You do not have permission to perform this action."},
        status=403,
    )


def portal_rate_limit_response(usage):
    return json_response(
        {
            "error": "rate_limit_exceeded",
            "message": "Monthly API key usage limit exceeded.",
            "usage": usage,
        },
        status=429,
    )


def get_authenticated_portal_user():
    user = get_portal_user_from_request()
    auth_error = _get_portal_auth_error()
    if auth_error:
        return None, auth_error
    if not user:
        return None, portal_unauthorized_response()
    return user, None


def get_portal_admin_from_request():
    user = get_portal_user_from_request()
    auth_error = _get_portal_auth_error()
    if auth_error:
        return None, auth_error
    if not user:
        return None, portal_unauthorized_response()
    if not is_portal_admin(user):
        return None, portal_forbidden_response()
    return user, None
