import hashlib
import hmac


def build_lazada_signature(api_path, params, app_secret):
    """Build the Lazada/TOP-style request signature.

    Algorithm (per Lazada Open Platform docs): sort all parameters
    (system + business, excluding `sign` itself) by key, concatenate each
    as `key` + `value` with no separator, prepend the API path, then
    HMAC-SHA256 with the app secret and uppercase-hex the digest.
    """
    base_string = api_path + "".join(
        f"{key}{params[key]}" for key in sorted(params.keys()) if key != "sign"
    )
    digest = hmac.new(
        (app_secret or "").encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest.upper()
