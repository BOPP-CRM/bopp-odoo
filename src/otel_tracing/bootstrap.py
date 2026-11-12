"""Install OpenTelemetry in an Odoo process.

Odoo with ``workers > 0`` preforks HTTP and cron workers and starts the
longpolling service as a separate ``gevent`` process. A BatchSpanProcessor
started in the parent dies across fork (its thread is not copied) and the
Python API refuses to replace the global tracer provider. So the parent
only installs library patches and a fork hook. Each worker creates its own
provider and exporter after fork. ``workers = 0`` and the gevent process
never fork workers, so they initialize immediately.

Nothing is patched unless OTEL_ENABLED is the string ``true`` and an OTLP
endpoint is set. Exporters run on a background batch thread; a dead
collector cannot fail or block a request.
"""

import logging
import os
import re

from .redact import sanitize_attributes, sanitize_url

_logger = logging.getLogger(__name__)

_PARTNER_SLUG = re.compile(r"^/api/partner/(?P<slug>[^/]+)(?P<rest>/.*)?$")
_TRACE_SUFFIX = " trace_id=%(otel_trace_id)s span_id=%(otel_span_id)s"
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 5000
_DEFAULT_SERVICE_NAME = "bopp-odoo"

# Set once per process. Forked children inherit the flags and must not patch
# a second time; they only create a provider via the fork hook.
_configured = False
_provider_pid = None
_active_exporter = None


def setup():
    """Enable tracing, or leave the process untouched when tracing is off."""
    global _configured
    if _configured:
        return
    _configured = True

    enabled, reason = _enabled_state()
    if not enabled:
        _logger.info("OpenTelemetry tracing is off (%s)", reason)
        return

    _install_log_correlation()
    _install_wsgi()
    _install_client_instrumentation()

    if _will_fork():
        _register_fork_hook()
        _logger.info(
            "OpenTelemetry tracing deferred until after worker fork pid=%s workers=%s",
            os.getpid(),
            _worker_count(),
        )
        return

    _init_provider()


def _enabled_state():
    """Return (enabled, reason). Enabled only for OTEL_ENABLED=true plus an endpoint."""
    flag = os.environ.get("OTEL_ENABLED", "").strip().lower()
    if flag != "true":
        return False, "OTEL_ENABLED is not true"
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False, "OTEL_SDK_DISABLED is true"
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    traces_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint and not traces_endpoint:
        return False, "OTEL_EXPORTER_OTLP_ENDPOINT is unset"
    return True, ""


def _worker_count():
    try:
        import odoo

        value = odoo.tools.config["workers"]
    except Exception:
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_evented():
    try:
        import odoo

        return bool(getattr(odoo, "evented", False))
    except Exception:
        return False


def _will_fork():
    """True only for the prefork parent, which forks HTTP and cron workers.

    The gevent longpolling process is a fresh interpreter (``odoo gevent``),
    so ``odoo.evented`` is already set and this process must trace itself.
    """
    return _worker_count() > 0 and not _is_evented()


def _register_fork_hook():
    if not hasattr(os, "register_at_fork"):
        _logger.warning("OpenTelemetry: os.register_at_fork is unavailable; workers will not trace")
        return

    def _after_fork():
        try:
            _init_provider()
        except Exception:
            _logger.exception("OpenTelemetry worker init failed; continuing without tracing")

    os.register_at_fork(after_in_child=_after_fork)


def _init_provider():
    """Create this process's tracer provider and batch exporter.

    Safe to call once per pid. The parent of a prefork server must not call
    it: set_tracer_provider sticks, and a BatchSpanProcessor thread does not
    survive fork. The SDK restarts a copied processor's thread, but gRPC
    channels and urllib pools from the parent are not fork-safe, so each
    worker builds its own exporter.
    """
    global _provider_pid, _active_exporter
    pid = os.getpid()
    if _provider_pid == pid:
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = _resource()
    # Sampler is read from OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG.
    provider = TracerProvider(resource=resource)
    exporter = _build_exporter()
    _active_exporter = exporter
    provider.add_span_processor(_RedactingSpanProcessor())
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            export_timeout_millis=_export_timeout_millis(),
        )
    )
    _force_set_tracer_provider(provider)
    _provider_pid = pid
    _logger.info(
        "OpenTelemetry tracing enabled pid=%s service=%s protocol=%s endpoint=%s",
        pid,
        resource.attributes.get("service.name"),
        _protocol(),
        _sanitized_endpoint(),
    )


def _resource():
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({})
    service_name = resource.attributes.get("service.name") or ""
    # Resource.create already applies OTEL_SERVICE_NAME and
    # OTEL_RESOURCE_ATTRIBUTES. Only fill the SDK's unknown_service default.
    if not service_name or str(service_name).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": _DEFAULT_SERVICE_NAME}))
    return resource


def _protocol():
    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "http/protobuf"
    )
    return raw.strip().lower()


def _sanitized_endpoint():
    endpoint = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    )
    return sanitize_url(endpoint.strip())


def _build_exporter():
    protocol = _protocol()
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()
    else:
        if protocol not in ("http/protobuf", "http/json"):
            _logger.warning(
                "OpenTelemetry protocol %r is not supported; using http/protobuf",
                protocol,
            )
        elif protocol == "http/json":
            _logger.warning(
                "OpenTelemetry http/json is not implemented by the Python exporter; using http/protobuf"
            )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()

    # grpcio uses its own sockets. Inside gevent that blocks the whole
    # longpolling hub. Run the export on gevent's real-thread pool instead.
    if protocol == "grpc" and _is_evented():
        exporter = _GeventThreadpoolExporter(exporter)
    return exporter


def _export_timeout_millis():
    raw = os.environ.get("OTEL_BSP_EXPORT_TIMEOUT", str(_DEFAULT_EXPORT_TIMEOUT_MILLIS))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_EXPORT_TIMEOUT_MILLIS


def _force_set_tracer_provider(provider):
    """Install ``provider`` even if a copied parent already set one.

    Fork copies the API's once-flag. Workers must replace it. The prefork
    parent never sets a provider; this reset covers a child that inherited
    a flag anyway.
    """
    import opentelemetry.trace as trace_api

    once = getattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and getattr(once, "_done", False):
        once._done = False
    trace_api.set_tracer_provider(provider)


class _GeventThreadpoolExporter:
    """Export spans from a real OS thread so grpc does not stall gevent."""

    def __init__(self, exporter):
        self._exporter = exporter

    def _call(self, method_name, *args):
        from gevent import get_hub

        method = getattr(self._exporter, method_name)
        return get_hub().threadpool.apply(method, args, {})

    def export(self, spans):
        try:
            return self._call("export", spans)
        except Exception:
            _logger.warning("OpenTelemetry export failed", exc_info=True)
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.FAILURE

    def shutdown(self, timeout_millis=30000):
        try:
            return self._call("shutdown")
        except Exception:
            _logger.debug("OpenTelemetry exporter shutdown failed", exc_info=True)
            return None

    def force_flush(self, timeout_millis=30000):
        try:
            return bool(self._call("force_flush", timeout_millis))
        except Exception:
            return False


class _RedactingSpanProcessor:
    """Remove secrets from a span after instrumentors have filled it in.

    Span attributes are frozen before ``on_end``, so this writes the
    underlying attribute dict directly. It runs before the batch processor,
    which exports that same span object.
    """

    def on_start(self, span, parent_context=None):
        return None

    def _on_ending(self, span):
        return None

    def on_end(self, span):
        try:
            _redact_ended_span(span)
        except Exception:
            _logger.debug("OpenTelemetry span redaction failed", exc_info=True)

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis=30000):
        return True


def _redact_ended_span(span):
    attributes = getattr(span, "_attributes", None)
    store = getattr(attributes, "_dict", None)
    if isinstance(store, dict):
        cleaned = sanitize_attributes(store)
        store.clear()
        store.update(cleaned)
    name = getattr(span, "_name", None)
    if isinstance(name, str) and ("?" in name or "@" in name):
        span._name = sanitize_url(name)


def _install_wsgi():
    """Wrap ``Root.__call__`` without replacing the Root instance.

    Odoo looks up ``root.session_store`` and other attributes on that
    instance. Special methods are resolved on the class, so the patch
    has to replace ``Root.__call__``. The server reads ``odoo.http.root``
    after this hook, and forked workers inherit the patched class.
    """
    try:
        import odoo.http as http
    except Exception:
        _logger.warning("OpenTelemetry: odoo.http is unavailable; HTTP server spans disabled")
        return

    root = getattr(http, "root", None)
    if root is None:
        _logger.warning("OpenTelemetry: odoo.http.root is missing; HTTP server spans disabled")
        return

    cls = type(root)
    if getattr(cls, "_bopp_otel_wrapped", False):
        return

    from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware

    original = cls.__call__

    def _application(environ, start_response):
        return original(root, environ, start_response)

    middleware = OpenTelemetryMiddleware(
        _application,
        request_hook=_wsgi_request_hook,
        response_hook=_wsgi_response_hook,
    )

    def traced_call(self, environ, start_response):
        return middleware(environ, start_response)

    cls.__call__ = traced_call
    cls._bopp_otel_wrapped = True


def _wsgi_request_hook(span, environ):
    try:
        if span is None or not span.is_recording():
            return
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD") or "HTTP"
        route = path
        match = _PARTNER_SLUG.match(path)
        if match:
            slug = match.group("slug")
            rest = match.group("rest") or ""
            route = "/api/partner/{slug}" + rest
            span.set_attribute("bopp.partner.slug", slug)
            span.update_name(f"{method} {route}")
        span.set_attribute("http.route", route)
    except Exception:
        _logger.debug("OpenTelemetry WSGI request hook failed", exc_info=True)


def _wsgi_response_hook(span, _environ, status, _response_headers):
    try:
        if span is None or not span.is_recording():
            return
        code_text = str(status).split(" ", 1)[0]
        span.set_attribute("http.response.status_code", int(code_text))
    except Exception:
        _logger.debug("OpenTelemetry WSGI response hook failed", exc_info=True)


def _install_client_instrumentation():
    _instrument(
        "psycopg2",
        "opentelemetry.instrumentation.psycopg2",
        "Psycopg2Instrumentor",
        # SQL text is kept (placeholders). Parameter values are not.
        capture_parameters=False,
        enable_commenter=False,
    )
    _instrument(
        "requests",
        "opentelemetry.instrumentation.requests",
        "RequestsInstrumentor",
    )
    _instrument(
        "urllib3",
        "opentelemetry.instrumentation.urllib3",
        "URLLib3Instrumentor",
    )


def _instrument(label, module_name, class_name, **kwargs):
    try:
        module = __import__(module_name, fromlist=[class_name])
        instrumentor_cls = getattr(module, class_name)
        instrumentor = instrumentor_cls()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(**kwargs)
    except Exception:
        _logger.exception("OpenTelemetry: %s instrumentation failed", label)


def _install_log_correlation():
    """Append trace_id and span_id to existing log formatters.

    Odoo installs its handlers before server-wide modules load. Forked
    workers inherit the patched formatters. Lines emitted outside a span
    record ``trace_id=-`` so the format never raises.
    """
    try:
        from opentelemetry import trace
    except Exception:
        _logger.exception("OpenTelemetry: log correlation unavailable")
        return

    class _TraceFilter(logging.Filter):
        def filter(self, record):
            _attach_trace_ids(record, trace)
            return True

    trace_filter = _TraceFilter()
    seen = set()
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        if not any(isinstance(item, _TraceFilter) for item in logger.filters):
            logger.addFilter(trace_filter)
        for handler in logger.handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            if not any(isinstance(item, _TraceFilter) for item in handler.filters):
                handler.addFilter(trace_filter)
            _extend_formatter(handler.formatter, trace)


def _attach_trace_ids(record, trace):
    trace_id = "-"
    span_id = "-"
    try:
        span = trace.get_current_span()
        context = span.get_span_context() if span is not None else None
        if context is not None and context.is_valid:
            trace_id = format(context.trace_id, "032x")
            span_id = format(context.span_id, "016x")
    except Exception:
        pass
    record.otel_trace_id = trace_id
    record.otel_span_id = span_id


def _extend_formatter(formatter, trace):
    if formatter is None or getattr(formatter, "_bopp_otel", False):
        return
    current = getattr(formatter, "_fmt", None)
    if not isinstance(current, str):
        return
    style = getattr(formatter, "_style", None)
    style_name = type(style).__name__ if style is not None else ""
    if style is not None and style_name not in ("PercentStyle",):
        return
    if "otel_trace_id" not in current:
        updated = current + _TRACE_SUFFIX
        formatter._fmt = updated
        if style is not None and hasattr(style, "_fmt"):
            style._fmt = updated
    original = formatter.format

    def format_with_trace(record):
        _attach_trace_ids(record, trace)
        return original(record)

    formatter.format = format_with_trace
    formatter._bopp_otel = True
