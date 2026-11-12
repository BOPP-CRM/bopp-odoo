"""Strip secrets from span attributes before they are exported.

OpenTelemetry's HTTP instrumentors record URLs and, when asked, headers.
This process calls LINE, SMS, and marketplace APIs with bearer tokens,
basic auth, and access tokens in query strings. Those values must not
leave the process on a span.
"""

import re
from urllib.parse import parse_qsl, urlsplit, urlencode, urlunsplit

# Sentinel: the attribute is removed rather than rewritten.
DROP = object()

# Query keys that carry credentials. Matched case-insensitively.
# `token`/`secret`/`password` match as substrings so access_token and
# X-Amz-Security-Token are covered. `key`, `sig`, and `auth` are whole
# segments so a parameter named `page` or `monkey` is kept.
_SENSITIVE_QUERY_KEY = re.compile(
    r"(token|secret|password|passwd|signature|credential|api[-_]?key|access[-_]?key|"
    r"(?:^|[._-])auth(?:$|[._-])|(?:^|[._-])key$|(?:^|[._-])sig$|jwt|session)",
    re.IGNORECASE,
)

_HEADER_ATTR = re.compile(
    r"^http\.(?:request|response)\.header\.(?P<name>.+)$",
    re.IGNORECASE,
)

_SENSITIVE_HEADER = re.compile(
    r"(authorization|cookie|set-cookie|proxy-authorization|api[-_]?key|token|secret|password|auth)",
    re.IGNORECASE,
)

_DB_PARAM_ATTR = re.compile(
    r"^db\.(?:statement\.parameters|query\.parameter)",
    re.IGNORECASE,
)

# Drop recorded bodies. Keep *.body.size, which is a length, not the body.
_BODY_ATTR = re.compile(r"(?:^|\.)(?:request|response)\.body(?:\.|$)", re.IGNORECASE)
_BODY_SIZE_ATTR = re.compile(r"\.body\.size$", re.IGNORECASE)

_URL_ATTRS = {
    "http.url",
    "http.target",
    "url.full",
    "url.query",
}


def is_sensitive_query_key(key):
    return bool(_SENSITIVE_QUERY_KEY.search(key or ""))


def redact_query(query):
    """Redact sensitive values in a raw query string, preserving other keys."""
    if not query or not isinstance(query, str):
        return query
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return query
    if not pairs:
        return query
    changed = False
    redacted = []
    for key, value in pairs:
        if is_sensitive_query_key(key):
            redacted.append((key, "REDACTED"))
            if value != "REDACTED":
                changed = True
        else:
            redacted.append((key, value))
    if not changed:
        return query
    return urlencode(redacted)


def sanitize_url(value):
    """Drop userinfo and sensitive query values from a URL or request target."""
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        _, _, host = netloc.rpartition("@")
        netloc = "REDACTED:REDACTED@" + host
    query = redact_query(parsed.query)
    if (
        netloc == parsed.netloc
        and query == parsed.query
        and not parsed.fragment
    ):
        return value
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def sanitize_attribute(key, value):
    """Return a replacement value, or DROP to remove the attribute."""
    if not isinstance(key, str):
        return value
    if _DB_PARAM_ATTR.search(key):
        return DROP
    if _BODY_ATTR.search(key) and not _BODY_SIZE_ATTR.search(key):
        return DROP
    header = _HEADER_ATTR.match(key)
    if header and _SENSITIVE_HEADER.search(header.group("name")):
        return DROP
    looks_like_url = (
        key in _URL_ATTRS
        or key.endswith(".url")
        or key.endswith("url.full")
        or key.endswith("url.query")
        or key == "http.target"
    )
    if not looks_like_url:
        return value
    query_only = key.endswith("query") or key == "url.query"
    return _sanitize_url_value(value, query_only=query_only)


def _sanitize_url_value(value, query_only):
    cleaner = redact_query if query_only else sanitize_url
    if isinstance(value, str):
        return cleaner(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        cleaned = tuple(cleaner(item) for item in value)
        if isinstance(value, list):
            return list(cleaned)
        return cleaned
    return value


def sanitize_attributes(attributes):
    """Copy ``attributes`` with secrets removed."""
    if not attributes:
        return {}
    cleaned = {}
    for key, value in attributes.items():
        new_value = sanitize_attribute(key, value)
        if new_value is DROP:
            continue
        cleaned[key] = new_value
    return cleaned
