"""Controller, ORM, custom-method, and cron spans for Odoo.

Installed from ``bootstrap.setup`` before workers fork. The patches are
plain function replacements: they do not start a tracer provider and they
do not register an ``os.register_at_fork`` hook. Each worker's provider
still comes from ``Worker.start``.

What is spanned
---------------
* One INTERNAL span per matched HTTP endpoint, named
  ``controller ClassName.method``, as a child of the WSGI server span.
  The server span's name and URL attributes are rewritten to the route
  pattern (``/api/integrations/zortout/<token>``).
* ORM entry points on every model: ``create``, ``write``, ``unlink``,
  ``search``, ``search_read``, and ``read``. The span name is
  ``res.partner.write`` with ``odoo.model``, ``odoo.method``, and
  ``odoo.record_count``.
* Public methods defined on models in BOPP addons under the extra-addons
  path (``crm_custom`` and any sibling addon except ``otel_tracing``).
  SQL issued by that method nests under it.
* One span per ``ir.cron`` job, named ``cron <job name>``, around
  ``ir.cron._callback``.

What is skipped
---------------
* Private and dunder methods (leading ``_``). That drops compute
  (``_compute_*``), inverse, onchange helpers, SQL constraints, and
  cron implementation helpers such as ``_cron_process_pending_jobs``.
  The cron *job* is still spanned via ``_callback``.
* Callables marked with ``_depends``, ``_onchange``, or ``_constrains``.
* Properties, fields, and lambdas.
* Recordset plumbing even if a custom addon redefines it: ``sudo``,
  ``with_context``, ``with_env``, ``with_user``, ``with_company``,
  ``with_prefetch``, ``browse``, ``exists``, ``ensure_one``, ``mapped``,
  ``filtered``, ``filtered_domain``, ``sorted``, ``grouped``, ``concat``,
  ``union``.
* Methods whose defining module is not a BOPP extra addon. Core ORM
  traffic is covered by the six entry points above, not by wrapping
  every ``res.partner`` method.
* Method spans are not started unless a parent span is already recording
  (an HTTP request or a cron job). Registry setup and module install
  therefore do not emit root spans.

Configuration (default on)
--------------------------
* ``OTEL_ODOO_METHOD_SPANS`` — ``true`` (default) or ``false``. Gates ORM
  methods, custom methods, and cron spans. Controller spans stay on
  whenever tracing is on.
* ``OTEL_ODOO_METHOD_ALLOWLIST`` — comma-separated model names
  (``res.partner``) or ``model.method`` pairs. Empty means all models.
  Does not filter controller or cron spans.
* ``OTEL_ODOO_METHOD_MIN_DURATION_MS`` — drop exported ORM and custom
  method spans shorter than this. Default ``0`` (keep them). Children of
  a dropped span keep their parent id, so leave this at 0 when the
  waterfall must stay intact.

Every hook fails open: an instrumentation error is logged once and the
original call still runs.
"""

import contextvars
import functools
import inspect
import logging
import os
import re
from pathlib import Path

from .redact import _http_method

_logger = logging.getLogger(__name__)

_ORM_METHODS = ("create", "write", "unlink", "search", "search_read", "read")
_SKIP_NAMES = frozenset(
    {
        "sudo",
        "with_context",
        "with_env",
        "with_user",
        "with_company",
        "with_prefetch",
        "browse",
        "exists",
        "ensure_one",
        "mapped",
        "filtered",
        "filtered_domain",
        "sorted",
        "grouped",
        "concat",
        "union",
    }
)
_PLACEHOLDER = re.compile(r"<(?:[^:>]+:)?[^>]+>")
_PARTNER_SLUG = re.compile(r"^/api/partner/(?P<slug>[^/]+)(?P<rest>/.*)?$")
_CRON_CALL = re.compile(r"\bmodel\.(\w+)\s*\(")

_RULE = contextvars.ContextVar("bopp_otel_http_rule", default=None)

_logged = set()
_config = None
_tracer_pid = None
_tracer_obj = None


def install_odoo_spans():
    """Patch dispatch, ORM entry points, and the registry. Never raises."""
    _install_dispatch()
    _install_orm()
    _install_registry_hook()


def reset_config():
    """Drop the cached env config. Tests call this after changing variables."""
    global _config
    _config = None


def method_min_duration_ns():
    config = _get_config()
    if config.min_ms <= 0:
        return 0
    return int(config.min_ms * 1_000_000)


def preliminary_route(path):
    """Best-effort pattern before Odoo has matched a route.

    Only ``/api/partner/<slug>/...`` is rewritten here. The dispatch hook
    replaces this with the real route pattern, including later segments.
    """
    if not isinstance(path, str) or not path:
        return None
    path = path.split("?", 1)[0]
    match = _PARTNER_SLUG.match(path)
    if not match:
        return None
    return "/api/partner/<slug>" + (match.group("rest") or "")


def normalize_route(route):
    """Turn ``/api/<string:token>`` into ``/api/<token>``."""
    if not isinstance(route, str):
        return route

    def _replace(match):
        body = match.group(0)[1:-1]
        name = body.split(":", 1)[-1]
        return f"<{name}>"

    return _PLACEHOLDER.sub(_replace, route)


def match_route(routes, path):
    """Return the normalized route pattern that matches ``path``."""
    if not path:
        return None
    path = path.split("?", 1)[0]
    best = None
    best_len = -1
    for route in routes or []:
        if not isinstance(route, str) or not route.startswith("/"):
            continue
        if _route_regex(route).fullmatch(path) and len(route) > best_len:
            best = route
            best_len = len(route)
    if best is None:
        return None
    return normalize_route(best)


def route_pattern(routes, path, rule=None):
    raw = getattr(rule, "rule", None) if rule is not None else None
    if isinstance(raw, str) and raw.startswith("/"):
        return normalize_route(raw.split("?", 1)[0])
    return match_route(routes, path)


class _Config:
    def __init__(self):
        flag = os.environ.get("OTEL_ODOO_METHOD_SPANS", "true").strip().lower()
        self.enabled = flag not in ("0", "false", "no", "off")
        raw = os.environ.get("OTEL_ODOO_METHOD_ALLOWLIST", "").strip()
        self.allow = None
        if raw:
            self.allow = {part.strip() for part in raw.split(",") if part.strip()}
        try:
            self.min_ms = float(os.environ.get("OTEL_ODOO_METHOD_MIN_DURATION_MS", "0") or 0)
        except (TypeError, ValueError):
            self.min_ms = 0.0


def _get_config():
    global _config
    if _config is None:
        _config = _Config()
    return _config


def _log_once(key, message):
    if key in _logged:
        return
    _logged.add(key)
    _logger.warning("%s", message, exc_info=True)


def _tracer():
    global _tracer_pid, _tracer_obj
    from opentelemetry import trace

    pid = os.getpid()
    if _tracer_obj is None or _tracer_pid != pid:
        _tracer_obj = trace.get_tracer("bopp.odoo")
        _tracer_pid = pid
    return _tracer_obj


def _parent_recording():
    from opentelemetry import trace

    parent = trace.get_current_span()
    return parent is not None and parent.is_recording()


def _mark_error(span, exc):
    try:
        if span is None or not span.is_recording():
            return
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code < 500:
            return
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR))
    except Exception:
        _log_once("span-status", "OpenTelemetry: failed to mark a span as error")


def _allowed(model_name, method_name):
    allow = _get_config().allow
    if not allow:
        return True
    return model_name in allow or f"{model_name}.{method_name}" in allow


def _start_span(name, attributes):
    return _tracer().start_as_current_span(name, attributes=attributes)


def _call_with_method_span(model_name, method_name, invoke, record=None, call_args=()):
    """Run ``invoke`` under ``{model}.{method}`` when method spans are on."""
    config = _get_config()
    if not config.enabled or not _allowed(model_name, method_name):
        return invoke()
    try:
        if not _parent_recording():
            return invoke()
    except Exception:
        _log_once("method-parent", "OpenTelemetry: method span parent check failed")
        return invoke()
    attributes = {
        "odoo.model": model_name,
        "odoo.method": method_name,
        "code.function": method_name,
        "bopp.span.group": "method",
    }
    try:
        cm = _start_span(f"{model_name}.{method_name}", attributes)
    except Exception:
        _log_once("method-start", "OpenTelemetry: method span failed to start")
        return invoke()
    with cm as span:
        try:
            result = invoke()
        except Exception as exc:
            _mark_error(span, exc)
            raise
        _set_record_count(span, method_name, record, call_args, result)
        return result


def _set_record_count(span, method_name, record, call_args, result):
    try:
        if span is None or not span.is_recording():
            return
        count = _record_count(method_name, record, call_args, result)
        if count is None:
            return
        span.set_attribute("odoo.record_count", int(count))
    except Exception:
        _log_once("method-count", "OpenTelemetry: record count attribute failed")


def _record_count(method_name, record, call_args, result):
    if method_name in ("write", "unlink", "read") and record is not None:
        return len(record)
    if method_name == "create":
        if result is not None:
            return len(result)
        vals = call_args[0] if call_args else None
        if isinstance(vals, (list, tuple)):
            return len(vals)
        return 1
    if method_name in ("search", "search_read") and result is not None:
        return len(result)
    if record is not None and hasattr(record, "_name"):
        try:
            count = len(record)
        except Exception:
            return None
        if count:
            return count
    return None


def _install_orm():
    try:
        import importlib

        models = importlib.import_module("odoo.models")
    except ModuleNotFoundError:
        return
    except Exception:
        _log_once("orm-import", "OpenTelemetry: ORM span hooks unavailable")
        return
    base = getattr(models, "BaseModel", None)
    if base is None:
        return
    try:
        patch_base_methods(base)
    except Exception:
        _log_once("orm-patch", "OpenTelemetry: ORM span hooks failed")


def patch_base_methods(base):
    """Wrap ORM entry points on ``base`` (Odoo's ``BaseModel``)."""
    for name in _ORM_METHODS:
        original = getattr(base, name, None)
        if original is None or getattr(original, "_bopp_otel_wrapped", False):
            continue
        setattr(base, name, _orm_wrapper(name, original))


def _orm_wrapper(method_name, original):
    def wrapper(self, *args, **kwargs):
        try:
            model_name = self._name or "unknown"
        except Exception:
            model_name = "unknown"

        def invoke():
            return original(self, *args, **kwargs)

        return _call_with_method_span(
            model_name,
            method_name,
            invoke,
            record=self,
            call_args=args,
        )

    _copy_wrap(wrapper, original)
    return wrapper


def _copy_wrap(wrapper, original):
    try:
        functools.update_wrapper(wrapper, original)
    except Exception:
        wrapper.__name__ = getattr(original, "__name__", "wrapped")
        wrapper.__doc__ = getattr(original, "__doc__", None)
    api_kind = getattr(original, "_api", None)
    if api_kind is not None:
        wrapper._api = api_kind
    wrapper._bopp_otel_wrapped = True
    return wrapper


def _install_dispatch():
    try:
        import importlib

        http = importlib.import_module("odoo.http")
    except ModuleNotFoundError:
        return
    except Exception:
        _log_once("http-import", "OpenTelemetry: controller spans unavailable")
        return
    if not hasattr(http, "Request"):
        # Unit tests install a stub odoo.http that is not the real server.
        return
    try:
        classes = []
        dispatcher = getattr(http, "Dispatcher", None)
        if dispatcher is not None:
            classes.extend(dispatcher.__subclasses__())
        for name in ("HttpDispatcher", "JsonRPCDispatcher"):
            cls = getattr(http, name, None)
            if cls is not None and cls not in classes:
                classes.append(cls)
        for cls in classes:
            _patch(cls, "dispatch", _dispatch_wrapper)
        request_cls = http.Request
        _patch(request_cls, "_serve_ir_http", _serve_ir_http_wrapper)
        _patch(request_cls, "_serve_nodb", _serve_nodb_wrapper)
    except Exception:
        _log_once("dispatch-patch", "OpenTelemetry: controller spans failed to install")


def _patch(cls, name, factory):
    original = getattr(cls, name, None)
    if original is None or getattr(original, "_bopp_otel", False):
        return
    wrapped = factory(original)
    wrapped._bopp_otel = True
    setattr(cls, name, wrapped)


def _serve_ir_http_wrapper(original):
    def _serve_ir_http(self, rule, args):
        try:
            token = _RULE.set(rule)
        except Exception:
            _log_once("rule-set", "OpenTelemetry: failed to remember the matched route")
            return original(self, rule, args)
        try:
            return original(self, rule, args)
        finally:
            try:
                _RULE.reset(token)
            except Exception:
                _log_once("rule-reset", "OpenTelemetry: failed to clear the matched route")

    return _serve_ir_http


def _serve_nodb_wrapper(original):
    def _serve_nodb(self):
        # The matched rule is local to the original method. Dispatch falls
        # back to endpoint.routing when this context var stays empty.
        try:
            token = _RULE.set(None)
        except Exception:
            return original(self)
        try:
            return original(self)
        finally:
            try:
                _RULE.reset(token)
            except Exception:
                _log_once("rule-reset", "OpenTelemetry: failed to clear the matched route")

    return _serve_nodb


def _dispatch_wrapper(original):
    def dispatch(self, endpoint, args):
        span_cm = None
        try:
            pattern = _pattern_for_dispatch(self, endpoint)
            _annotate_server_span(pattern)
            span_cm = _controller_cm(endpoint, pattern)
        except Exception:
            _log_once("controller-start", "OpenTelemetry: controller span failed to start")
            span_cm = None
        if span_cm is None:
            return original(self, endpoint, args)
        with span_cm as span:
            try:
                return original(self, endpoint, args)
            except Exception as exc:
                _mark_error(span, exc)
                raise

    return dispatch


def _request_path(dispatcher):
    try:
        return dispatcher.request.httprequest.path or "/"
    except Exception:
        return "/"


def _endpoint_routes(endpoint):
    routing = getattr(endpoint, "routing", None)
    if not isinstance(routing, dict):
        func = getattr(endpoint, "func", None)
        routing = getattr(func, "routing", None) or getattr(func, "original_routing", None)
    if not isinstance(routing, dict):
        return []
    routes = routing.get("routes") or []
    if isinstance(routes, str):
        return [routes]
    return list(routes)


def _pattern_for_dispatch(dispatcher, endpoint):
    try:
        rule = _RULE.get()
    except Exception:
        rule = None
    pattern = route_pattern(_endpoint_routes(endpoint), _request_path(dispatcher), rule)
    if pattern:
        return pattern
    return preliminary_route(_request_path(dispatcher))


def _annotate_server_span(pattern):
    if not pattern:
        return
    from opentelemetry import trace

    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return
    span.set_attribute("http.route", pattern)
    method = "HTTP"
    try:
        attributes = dict(span.attributes or {})
        method = _method_from_span(span, attributes)
    except Exception:
        method = "HTTP"
    span.update_name(f"{method} {pattern}")
    for key in ("http.target", "url.path"):
        span.set_attribute(key, pattern)


def _method_from_span(span, attributes):
    name = getattr(span, "name", None)
    return _http_method(name, attributes)


def _controller_identity(endpoint):
    func = endpoint
    if isinstance(endpoint, functools.partial):
        func = endpoint.func
    owner = getattr(func, "__self__", None)
    method_name = getattr(func, "__name__", None) or "endpoint"
    if owner is None:
        return "endpoint", method_name, method_name
    cls = owner.__class__
    class_name = getattr(cls, "__name__", "endpoint")
    namespace = f"{getattr(cls, '__module__', '')}.{class_name}".strip(".")
    return class_name, namespace, method_name


def _controller_cm(endpoint, pattern):
    class_name, namespace, method_name = _controller_identity(endpoint)
    attributes = {
        "code.namespace": namespace,
        "code.function": method_name,
        "bopp.span.group": "controller",
    }
    if pattern:
        attributes["http.route"] = pattern
    return _start_span(f"controller {class_name}.{method_name}", attributes)


def _route_regex(route):
    parts = []
    position = 0
    for match in _PLACEHOLDER.finditer(route):
        parts.append(re.escape(route[position : match.start()]))
        parts.append(r"[^/]+")
        position = match.end()
    parts.append(re.escape(route[position:]))
    return re.compile("".join(parts) + "/?")


def _install_registry_hook():
    try:
        import importlib

        registry_mod = importlib.import_module("odoo.modules.registry")
    except ModuleNotFoundError:
        return
    except Exception:
        _log_once("registry-import", "OpenTelemetry: model span hooks unavailable")
        return
    registry_cls = getattr(registry_mod, "Registry", None)
    if registry_cls is None or getattr(registry_cls, "_bopp_otel_registry", False):
        return
    try:
        original_new = registry_cls.new
        original_setup = registry_cls.setup_models

        def new(cls, *args, **kwargs):
            registry = original_new(*args, **kwargs)
            try:
                instrument_registry(registry)
            except Exception:
                _log_once("registry", "OpenTelemetry: model span hooks failed")
            return registry

        def setup_models(self, cursor):
            result = original_setup(self, cursor)
            try:
                if getattr(self, "ready", False):
                    instrument_registry(self)
            except Exception:
                _log_once("registry", "OpenTelemetry: model span hooks failed")
            return result

        new._bopp_otel = True
        registry_cls.new = classmethod(new)
        registry_cls.setup_models = setup_models
        registry_cls._bopp_otel_registry = True
    except Exception:
        _log_once("registry-patch", "OpenTelemetry: model span hooks failed to install")


def instrument_registry(registry):
    """Wrap custom-addon methods and ir.cron on a loaded registry."""
    custom = custom_addon_names()
    names = list(registry)
    for model_name in names:
        try:
            model_cls = registry[model_name]
        except Exception:
            continue
        if not isinstance(model_cls, type):
            continue
        try:
            wrap_model_methods(model_cls, custom)
        except Exception:
            _log_once(
                f"model:{getattr(model_cls, '_name', model_name)}",
                "OpenTelemetry: failed to wrap methods on a model",
            )
    if "ir.cron" in registry:
        try:
            wrap_cron(registry["ir.cron"])
        except Exception:
            _log_once("cron", "OpenTelemetry: cron span hook failed")


def custom_addon_names():
    """Addon directory names that live outside Odoo's own addons tree."""
    try:
        import odoo
        import odoo.addons
    except Exception:
        return set()
    try:
        odoo_addons = Path(odoo.__file__).resolve().parent / "addons"
    except Exception:
        odoo_addons = None
    names = set()
    for entry in list(getattr(odoo.addons, "__path__", []) or []):
        try:
            root = Path(entry).resolve()
        except OSError:
            continue
        if odoo_addons is not None and root == odoo_addons:
            continue
        if (root / "__manifest__.py").is_file() or (root / "__openerp__.py").is_file():
            if root.name != "otel_tracing":
                names.add(root.name)
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name in ("otel_tracing", "__pycache__") or child.name.startswith("."):
                continue
            if (child / "__manifest__.py").is_file() or (child / "__openerp__.py").is_file():
                names.add(child.name)
    return names


def wrap_model_methods(model_cls, custom_modules):
    """Wrap the most specific public custom-addon method for each name."""
    if not custom_modules:
        return
    model_name = getattr(model_cls, "_name", None)
    if not isinstance(model_name, str) or not model_name:
        return
    seen = set()
    for klass in model_cls.__mro__:
        for name, attr in list(klass.__dict__.items()):
            if name in seen:
                continue
            func, kind = _peek_callable(attr)
            if func is None:
                continue
            seen.add(name)
            if getattr(func, "_bopp_otel_wrapped", False):
                continue
            if not _should_trace_custom(name, func, custom_modules):
                continue
            setattr(model_cls, name, _method_wrapper(model_name, name, func, kind))


def wrap_cron(model_cls):
    attr = None
    for klass in model_cls.__mro__:
        if "_callback" in klass.__dict__:
            attr = klass.__dict__["_callback"]
            break
    func, kind = _peek_callable(attr)
    if func is None or kind != "function" or getattr(func, "_bopp_otel_cron", False):
        return
    wrapped = _cron_wrapper(func)
    setattr(model_cls, "_callback", wrapped)


def _peek_callable(attr):
    if isinstance(attr, staticmethod):
        return attr.__func__, "static"
    if isinstance(attr, classmethod):
        return attr.__func__, "class"
    if inspect.isfunction(attr):
        return attr, "function"
    return None, None


def _should_trace_custom(name, func, custom_modules):
    if not name or name.startswith("_") or name.startswith("<") or name in _SKIP_NAMES:
        return False
    if getattr(func, "_depends", None) or getattr(func, "_onchange", None) or getattr(func, "_constrains", None):
        return False
    module = getattr(func, "__module__", "") or ""
    addon = _addon_of(module)
    return addon in custom_modules


def _addon_of(module_name):
    parts = module_name.split(".")
    if len(parts) >= 3 and parts[0] == "odoo" and parts[1] == "addons":
        return parts[2]
    return None


def _method_wrapper(model_name, method_name, func, kind):
    if kind == "static":

        def wrapper(*args, **kwargs):
            def invoke():
                return func(*args, **kwargs)

            return _call_with_method_span(model_name, method_name, invoke)

        _copy_wrap(wrapper, func)
        return staticmethod(wrapper)

    def wrapper(self, *args, **kwargs):
        def invoke():
            return func(self, *args, **kwargs)

        return _call_with_method_span(
            model_name,
            method_name,
            invoke,
            record=self,
            call_args=args,
        )

    _copy_wrap(wrapper, func)
    if kind == "class":
        return classmethod(wrapper)
    return wrapper


def _cron_wrapper(func):
    def _callback(self, cron_name, server_action_id, *args, **kwargs):
        if not _get_config().enabled:
            return func(self, cron_name, server_action_id, *args, **kwargs)
        try:
            label = cron_name if isinstance(cron_name, str) and cron_name else "job"
            if len(label) > 180:
                label = label[:180]
            cm = _start_span(
                f"cron {label}",
                {
                    "odoo.cron.name": label,
                    "bopp.span.group": "cron",
                    "code.function": "_callback",
                    "code.namespace": "odoo.addons.base.models.ir_cron.ir_cron",
                },
            )
        except Exception:
            _log_once("cron-start", "OpenTelemetry: cron span failed to start")
            return func(self, cron_name, server_action_id, *args, **kwargs)
        with cm as span:
            _annotate_cron_method(span, self, server_action_id)
            try:
                return func(self, cron_name, server_action_id, *args, **kwargs)
            except Exception as exc:
                _mark_error(span, exc)
                raise

    _copy_wrap(_callback, func)
    _callback._bopp_otel_cron = True
    return _callback


def _annotate_cron_method(span, record, server_action_id):
    try:
        if span is None or not span.is_recording():
            return
        action = record.env["ir.actions.server"].sudo().browse(server_action_id)
        code = action.code or ""
        match = _CRON_CALL.search(code)
        method = match.group(1) if match else ""
        model = ""
        try:
            model = action.model_id.model or ""
        except Exception:
            model = ""
        qualified = f"{model}.{method}" if model and method else method
        if qualified:
            span.set_attribute("odoo.cron.method", qualified)
    except Exception:
        _log_once("cron-method", "OpenTelemetry: cron method attribute failed")


# Imported for tests that want a stable reference without going through Odoo.
__all__ = [
    "custom_addon_names",
    "install_odoo_spans",
    "instrument_registry",
    "match_route",
    "method_min_duration_ns",
    "normalize_route",
    "patch_base_methods",
    "preliminary_route",
    "reset_config",
    "route_pattern",
    "wrap_cron",
    "wrap_model_methods",
]
