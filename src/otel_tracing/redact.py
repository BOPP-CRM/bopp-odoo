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


_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}

# Path and URL keys a server span must show as the route pattern, not the
# raw request target. Query strings are dropped from these on server spans.
_SERVER_PATH_ATTRS = {"http.target", "url.path"}
_SERVER_URL_ATTRS = {"http.url", "url.full"}


def strip_query(value):
    """Drop the query string and fragment from a URL or request target."""
    if not isinstance(value, str) or "?" not in value and "#" not in value:
        return value
    return value.split("?", 1)[0].split("#", 1)[0]


def replace_url_path(value, path):
    """Replace the path of a URL with ``path`` and drop the query string."""
    if not isinstance(value, str):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return path
    if not parsed.scheme and not parsed.netloc:
        return path
    netloc = parsed.netloc
    if "@" in netloc:
        _, _, host = netloc.rpartition("@")
        netloc = "REDACTED:REDACTED@" + host
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _http_method(name, attributes):
    for key in ("http.request.method", "http.method"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    if isinstance(name, str) and " " in name:
        first = name.split(" ", 1)[0].strip().upper()
        if first in _HTTP_METHODS:
            return first
    return "HTTP"


def sanitize_span_name(name):
    """Remove query strings and URL userinfo from a span name."""
    if not isinstance(name, str):
        return name
    name = strip_query(name).rstrip()
    if "://" not in name and "@" not in name:
        return name
    parts = []
    for part in name.split(" "):
        if "://" in part or "@" in part:
            parts.append(sanitize_url(strip_query(part)))
        else:
            parts.append(part)
    return " ".join(parts)


def _rewrite_server_value(key, value, route):
    if key in _SERVER_PATH_ATTRS or key.endswith("url.path") or key == "http.target":
        return route
    if key in _SERVER_URL_ATTRS or key == "http.url" or key.endswith("url.full"):
        if isinstance(value, str):
            return replace_url_path(value, route)
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            rewritten = [replace_url_path(item, route) for item in value]
            return type(value)(rewritten)
    return value


def finalize_exported_span(name, attributes, is_server):
    """Apply route-pattern and query redaction to a span about to be exported.

    Server spans are renamed to ``METHOD <route pattern>`` and their URL
    attributes use that pattern with the query string removed. Client spans
    keep non-sensitive query parameters and lose sensitive ones via
    ``sanitize_attributes``. ``bopp.partner.slug`` is never exported: the
    slug is a path parameter and belongs only inside ``<slug>``.
    """
    cleaned = sanitize_attributes(attributes)
    cleaned.pop("bopp.partner.slug", None)
    if not is_server:
        return sanitize_span_name(name), cleaned

    route = cleaned.get("http.route")
    if isinstance(route, str) and route.startswith("/"):
        route = strip_query(route)
        cleaned["http.route"] = route
        name = f"{_http_method(name, cleaned)} {route}"
        for key in list(cleaned):
            cleaned[key] = _rewrite_server_value(key, cleaned[key], route)
        cleaned.pop("url.query", None)
        cleaned.pop("http.query", None)
        return name, cleaned

    name = sanitize_span_name(name)
    for key in ("http.target", "http.url", "url.full", "url.path"):
        value = cleaned.get(key)
        if isinstance(value, str):
            cleaned[key] = strip_query(sanitize_url(value))
    cleaned.pop("url.query", None)
    cleaned.pop("http.query", None)
    return name, cleaned
