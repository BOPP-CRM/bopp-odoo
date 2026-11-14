"""Install OpenTelemetry in an Odoo process.

Odoo with ``workers > 0`` preforks HTTP and cron workers and starts the
longpolling service as a separate ``gevent`` process. A BatchSpanProcessor
started in the parent dies across fork (its thread is not copied) and the
Python API refuses to replace the global tracer provider. So the parent
only installs library patches. Each worker creates its own provider after
``os.fork`` returns. ``workers = 0`` and the gevent process never fork
workers, so they initialize immediately.

The provider is not created from ``os.register_at_fork``. CPython runs
``after_in_child`` hooks in registration order and ``before`` hooks in
reverse order. ``concurrent.futures.thread`` (imported while Odoo preloads
the registry, via crm_custom -> boto3) acquires a non-reentrant lock in its
``before`` hook. An earlier ``after_in_child`` hook that then calls
``ThreadPoolExecutor.submit`` waits on that lock forever, so the worker
never returns from ``fork`` and never accepts HTTP. ``Worker.start`` runs
only after every at-fork hook has finished.

Init is fail-open: errors are logged and a hung init is abandoned after
``OTEL_INIT_TIMEOUT`` seconds (default 3). The worker serves either way.

Nothing is patched unless OTEL_ENABLED is the string ``true`` and an OTLP
endpoint is set. Exporters run on a background batch thread; a dead
collector cannot fail or block a request.
"""

import logging
import os
import sys

from .redact import finalize_exported_span, sanitize_url

_logger = logging.getLogger(__name__)

_TRACE_ID_HEADER = "X-Trace-Id"
_EXPOSE_HEADER = "Access-Control-Expose-Headers"
_ALLOW_ORIGIN_HEADER = "Access-Control-Allow-Origin"
_warned_once = set()

_TRACE_SUFFIX = " trace_id=%(otel_trace_id)s span_id=%(otel_span_id)s"
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 5000
_DEFAULT_INIT_TIMEOUT_SEC = 3.0
_DEFAULT_SERVICE_NAME = "bopp-odoo"

# Set once per process. Forked children inherit the flags and must not patch
# a second time; they only create a provider from Worker.start.
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
    _install_cors_expose()
    _install_client_instrumentation()
    try:
        from .spans import install_odoo_spans

        install_odoo_spans()
    except Exception:
        _logger.warning(
            "OpenTelemetry: Odoo span hooks failed; continuing without them",
            exc_info=True,
        )

    if _will_fork():
        _install_worker_hook()
        _logger.info(
            "OpenTelemetry tracing deferred until after worker fork pid=%s workers=%s",
            os.getpid(),
            _worker_count(),
        )
        return

    _safe_init_provider()


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


def _init_timeout_sec():
    raw = os.environ.get("OTEL_INIT_TIMEOUT", str(_DEFAULT_INIT_TIMEOUT_SEC))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_INIT_TIMEOUT_SEC


def _thread_primitives():
    """Real OS thread primitives, even when gevent has patched threading."""
    try:
        from gevent import monkey

        if monkey.is_module_patched("threading"):
            thread_cls = monkey.get_original("threading", "Thread")
            event_cls = monkey.get_original("threading", "Event")
            return thread_cls, event_cls
    except Exception:
        pass
    import threading

    return threading.Thread, threading.Event


def _install_worker_hook():
    """Create the provider from ``Worker.start``, after ``os.fork`` returns.

    HTTP and cron workers both go through ``Worker.start``. The gevent
    process is not a ``Worker``. A missing hook only disables tracing.
    """
    try:
        from odoo.service.server import Worker
    except Exception:
        _logger.warning(
            "OpenTelemetry: odoo.service.server.Worker is unavailable; workers will not trace",
            exc_info=True,
        )
        return

    if getattr(Worker.start, "_bopp_otel", False):
        return

    original = Worker.start

    def start(self, *args, **kwargs):
        _safe_init_provider()
        return original(self, *args, **kwargs)

    start._bopp_otel = True
    Worker.start = start


def _safe_init_provider():
    """Initialize tracing without blocking startup if init fails or hangs."""
    try:
        if _provider_pid == os.getpid():
            return
        _logger.info(
            "OpenTelemetry worker init starting pid=%s threadpool_preloaded=%s",
            os.getpid(),
            "concurrent.futures.thread" in sys.modules,
        )
        thread_cls, event_cls = _thread_primitives()
        done = event_cls()
        box = {}

        def _run():
            try:
                _init_provider()
            except Exception as exc:
                box["exc"] = exc
            finally:
                done.set()

        thread = thread_cls(target=_run, name="bopp-otel-init", daemon=True)
        thread.start()
        timeout = _init_timeout_sec()
        if not done.wait(timeout):
            _logger.warning(
                "OpenTelemetry init timed out after %ss; continuing without waiting pid=%s",
                timeout,
                os.getpid(),
            )
            return
        exc = box.get("exc")
        if exc is not None:
            _logger.warning(
                "OpenTelemetry init failed; continuing without tracing pid=%s",
                os.getpid(),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
    except Exception:
        _logger.warning(
            "OpenTelemetry init failed; continuing without tracing pid=%s",
            os.getpid(),
            exc_info=True,
        )


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
    # Redaction mutates the span, then the duration filter decides whether
    # the batch processor exports it. The batch processor is not registered
    # on its own, so a dropped method span is not exported.
    batch = BatchSpanProcessor(
        exporter,
        export_timeout_millis=_export_timeout_millis(),
    )
    provider.add_span_processor(_RedactingSpanProcessor())
    provider.add_span_processor(_MethodDurationFilter(batch, _method_min_duration_ns()))
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


class _MethodDurationFilter:
    """Forward spans to the batch processor, except short method spans.

    Controller, server, client, SQL, and cron spans are always forwarded.
    A minimum of 0 (the default) forwards everything.
    """

    def __init__(self, downstream, min_ns):
        self._downstream = downstream
        self._min_ns = min_ns

    def on_start(self, span, parent_context=None):
        start = getattr(self._downstream, "on_start", None)
        if start is not None:
            start(span, parent_context)

    def _on_ending(self, span):
        # The SDK calls this on every processor before on_end. The batch
        # processor is wrapped, so it only sees the call if we forward it.
        ending = getattr(self._downstream, "_on_ending", None)
        if ending is not None:
            ending(span)

    def on_end(self, span):
        try:
            if self._min_ns > 0 and _is_short_method_span(span, self._min_ns):
                return
        except Exception:
            _logger.debug("OpenTelemetry method-span filter failed", exc_info=True)
        self._downstream.on_end(span)

    def shutdown(self):
        return self._downstream.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self._downstream.force_flush(timeout_millis)


def _is_short_method_span(span, min_ns):
    attributes = getattr(span, "attributes", None) or {}
    if attributes.get("bopp.span.group") != "method":
        return False
    start = getattr(span, "start_time", None)
    end = getattr(span, "end_time", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return (end - start) < min_ns


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


def _is_server_span(span):
    kind = getattr(span, "kind", None)
    if getattr(kind, "name", None) == "SERVER":
        return True
    return getattr(kind, "value", None) == 1


def _method_min_duration_ns():
    try:
        from .spans import method_min_duration_ns

        return method_min_duration_ns()
    except Exception:
        return 0


def _redact_ended_span(span):
    is_server = _is_server_span(span)
    attributes = getattr(span, "_attributes", None)
    store = getattr(attributes, "_dict", None)
    current = dict(store) if isinstance(store, dict) else {}
    name, cleaned = finalize_exported_span(getattr(span, "_name", None), current, is_server)
    if isinstance(store, dict):
        store.clear()
        store.update(cleaned)
    if isinstance(name, str):
        span._name = name
    _redact_events(span, is_server, cleaned.get("http.route") if is_server else None)


def _redact_events(span, is_server, route):
    events = getattr(span, "_events", None)
    if not events:
        return
    from .redact import replace_url_path, sanitize_attributes

    for event in events:
        attrs = getattr(event, "attributes", None)
        event_store = getattr(attrs, "_dict", None)
        if not isinstance(event_store, dict):
            continue
        cleaned = sanitize_attributes(event_store)
        cleaned.pop("bopp.partner.slug", None)
        if is_server and isinstance(route, str) and route.startswith("/"):
            for key in list(cleaned):
                if key in ("http.target", "url.path") or key == "http.url" or key == "url.full":
                    value = cleaned[key]
                    if key in ("http.target", "url.path") and isinstance(value, str):
                        cleaned[key] = route
                    elif isinstance(value, str):
                        cleaned[key] = replace_url_path(value, route)
            cleaned.pop("url.query", None)
        event_store.clear()
        event_store.update(cleaned)


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
        from .spans import preliminary_route

        path = (environ.get("PATH_INFO") or "/").split("?", 1)[0]
        method = environ.get("REQUEST_METHOD") or "HTTP"
        route = preliminary_route(path)
        # Query strings never belong in the server span name. The dispatch
        # hook replaces this with the matched route pattern when Odoo has one.
        span.update_name(f"{method} {route or path}")
        if route:
            span.set_attribute("http.route", route)
            span.set_attribute("http.target", route)
            span.set_attribute("url.path", route)
    except Exception:
        _logger.debug("OpenTelemetry WSGI request hook failed", exc_info=True)


def _wsgi_response_hook(span, _environ, status, response_headers):
    """Record the status code and add ``X-Trace-Id`` when this span has one.

    The header is the server span's 32-hex trace id. It is added for a
    valid context that is recording or sampled, including error and
    redirect responses, because those still pass through ``start_response``.
    An existing ``X-Trace-Id`` is left alone. ``traceresponse`` is not set.
    A failure here is logged once and does not stop the response.
    """
    try:
        if span is not None and span.is_recording():
            code_text = str(status).split(" ", 1)[0]
            span.set_attribute("http.response.status_code", int(code_text))
    except Exception:
        _logger.debug("OpenTelemetry WSGI response hook failed", exc_info=True)
    try:
        _attach_trace_response_header(span, response_headers)
    except Exception:
        _warn_once(
            "trace-header",
            "OpenTelemetry: failed to add X-Trace-Id response header",
        )


def _warn_once(key, message):
    if key in _warned_once:
        return
    _warned_once.add(key)
    _logger.warning("%s", message, exc_info=True)


def _names_match(left, right):
    return str(left).lower() == str(right).lower()


def _header_pairs(headers):
    if isinstance(headers, (list, tuple)):
        pairs = headers
    elif hasattr(headers, "items"):
        pairs = headers.items()
    else:
        pairs = headers
    for item in pairs:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            yield item[0], item[1]


def _has_header(headers, name):
    return any(_names_match(key, name) for key, _value in _header_pairs(headers))


def _trace_id_listed(value):
    if value is None:
        return False
    return any(
        part.strip().lower() == _TRACE_ID_HEADER.lower()
        for part in str(value).split(",")
        if part.strip()
    )


def _add_header(headers, name, value):
    if isinstance(headers, list):
        headers.append((name, value))
        return
    if hasattr(headers, "add"):
        headers.add(name, value)
        return
    headers.append((name, value))


def _expose_trace_id(headers):
    """Let browsers read ``X-Trace-Id`` on responses that already send CORS.

    ``Access-Control-Allow-Origin`` is set by Odoo for routes that pass
    ``cors=`` (the API controllers do). ``Access-Control-Expose-Headers``
    is created or extended so the new name sits next to any names already
    listed. Responses without an allow-origin header are left unchanged.
    """
    if headers is None or not _has_header(headers, _ALLOW_ORIGIN_HEADER):
        return
    expose_values = [
        value
        for key, value in _header_pairs(headers)
        if _names_match(key, _EXPOSE_HEADER)
    ]
    if any(_trace_id_listed(value) for value in expose_values):
        return
    if not expose_values:
        _add_header(headers, _EXPOSE_HEADER, _TRACE_ID_HEADER)
        return
    if not isinstance(headers, (list, tuple)) and hasattr(headers, "set"):
        combined = ", ".join(
            str(value).strip() for value in expose_values if str(value).strip()
        )
        merged = f"{combined}, {_TRACE_ID_HEADER}" if combined else _TRACE_ID_HEADER
        headers.set(_EXPOSE_HEADER, merged)
        return
    current = expose_values[0]
    merged = (
        f"{str(current).rstrip()}, {_TRACE_ID_HEADER}"
        if str(current).strip()
        else _TRACE_ID_HEADER
    )
    for index, (key, _current) in enumerate(headers):
        if _names_match(key, _EXPOSE_HEADER):
            headers[index] = (key, merged)
            return
    _add_header(headers, _EXPOSE_HEADER, merged)


def _server_trace_id(span):
    """32-hex trace id, or None when this response should not advertise one."""
    if span is None:
        return None
    context = span.get_span_context()
    if context is None or not getattr(context, "is_valid", False):
        return None
    recording = False
    is_recording = getattr(span, "is_recording", None)
    if callable(is_recording):
        recording = bool(is_recording())
    sampled = bool(getattr(getattr(context, "trace_flags", None), "sampled", False))
    if not recording and not sampled:
        return None
    trace_id = getattr(context, "trace_id", 0)
    if not isinstance(trace_id, int) or trace_id <= 0:
        return None
    return format(trace_id, "032x")


def _attach_trace_response_header(span, response_headers):
    if response_headers is None:
        return
    trace_id = _server_trace_id(span)
    if not trace_id:
        return
    if not _has_header(response_headers, _TRACE_ID_HEADER):
        _add_header(response_headers, _TRACE_ID_HEADER, trace_id)
    _expose_trace_id(response_headers)


def _install_cors_expose():
    """Merge ``X-Trace-Id`` into CORS expose headers as Odoo sets them.

    Custom API routes pass ``cors=`` to ``@http.route``. Odoo writes
    ``Access-Control-Allow-Origin`` from ``Dispatcher.pre_dispatch``,
    including the OPTIONS preflight that aborts before the controller.
    The wrapper runs after that write and still runs when preflight
    raises. A missing dispatcher only skips this patch; the response
    hook still exposes the header when the finished response has
    allow-origin.
    """
    try:
        import odoo.http as http
    except Exception:
        _warn_once(
            "cors-import",
            "OpenTelemetry: could not import odoo.http for CORS expose header",
        )
        return

    dispatcher = getattr(http, "Dispatcher", None)
    current = getattr(dispatcher, "pre_dispatch", None) if dispatcher is not None else None
    if current is None or getattr(current, "_bopp_otel", False):
        return
    original = current

    def pre_dispatch(self, rule, args):
        try:
            return original(self, rule, args)
        finally:
            try:
                _expose_trace_id(self.request.future_response.headers)
            except Exception:
                _warn_once(
                    "cors-expose",
                    "OpenTelemetry: failed to expose X-Trace-Id on a CORS response",
                )

    pre_dispatch._bopp_otel = True
    dispatcher.pre_dispatch = pre_dispatch


def _client_request_hook(span, *args, **kwargs):
    """Redact the outbound URL as soon as the client span starts.

    ``on_end`` redaction is the backstop. This hook covers instrumentors
    that copy the URL into the span name before the request returns.
    Headers and bodies are not recorded.
    """
    try:
        if span is None or not getattr(span, "is_recording", lambda: False)():
            return
        url = None
        for arg in args:
            candidate = getattr(arg, "url", None)
            if candidate is None and isinstance(arg, str) and "://" in arg:
                candidate = arg
            if candidate:
                url = candidate
                break
        if isinstance(url, bytes):
            url = url.decode("latin1", "replace")
        if isinstance(url, str):
            cleaned = sanitize_url(url)
            span.set_attribute("http.url", cleaned)
            span.set_attribute("url.full", cleaned)
    except Exception:
        _logger.debug("OpenTelemetry client request hook failed", exc_info=True)


def _install_client_instrumentation():
    _instrument(
        "psycopg2",
        "opentelemetry.instrumentation.psycopg2",
        "Psycopg2Instrumentor",
        # SQL text is kept (placeholders). Bound parameter values are not.
        capture_parameters=False,
        enable_commenter=False,
    )
    client_hook = {"request_hook": _client_request_hook}
    _instrument(
        "requests",
        "opentelemetry.instrumentation.requests",
        "RequestsInstrumentor",
        **client_hook,
    )
    _instrument(
        "urllib3",
        "opentelemetry.instrumentation.urllib3",
        "URLLib3Instrumentor",
        **client_hook,
    )
    # httpx is not imported by this image today. Instrument it only when
    # the library is actually installed so startup does not log a failure.
    try:
        import importlib.util

        httpx_installed = importlib.util.find_spec("httpx") is not None
    except Exception:
        httpx_installed = False
    if httpx_installed:
        _instrument(
            "httpx",
            "opentelemetry.instrumentation.httpx",
            "HTTPXClientInstrumentor",
            **client_hook,
        )


def _instrument(label, module_name, class_name, **kwargs):
    try:
        module = __import__(module_name, fromlist=[class_name])
        instrumentor_cls = getattr(module, class_name)
        instrumentor = instrumentor_cls()
        if instrumentor.is_instrumented_by_opentelemetry:
            return
        try:
            instrumentor.instrument(**kwargs)
        except TypeError:
            # Older instrumentors reject request_hook. Retry without extras
            # rather than leaving the library uninstrumented.
            instrumentor.instrument()
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
