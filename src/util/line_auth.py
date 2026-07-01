import requests

from .portal_auth import get_bearer_token
from .request import json_response

LINE_PROFILE_URL = "https://api.line.me/v2/profile"


def get_line_profile_from_access_token(access_token):
    if not access_token:
        return None

    try:
        response = requests.get(
            LINE_PROFILE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    profile = response.json()
    if not profile.get("userId"):
        return None

    return profile


def get_line_profile_from_request():
    access_token = get_bearer_token()
    if not access_token:
        return None, line_unauthorized_response()

    profile = get_line_profile_from_access_token(access_token)
    if not profile:
        return None, line_unauthorized_response()

    return profile, None


def line_unauthorized_response():
    return json_response(
        {"error": "unauthorized", "message": "Invalid or missing LINE access token."},
        status=401,
    )
