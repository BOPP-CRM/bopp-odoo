"""OpenTelemetry wiring tests.

Redaction and odoo.conf editing run in-process. Anything that patches
libraries or forks runs in a subprocess so cases do not share a tracer
provider.
"""

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _clean_env(**extra):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OTEL_"):
            del env[key]
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def run_scenario(name, env, timeout=60):
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--scenario", name],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"scenario {name} failed ({proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


class RedactTests(unittest.TestCase):
    def test_redacts_tokens_and_userinfo_but_keeps_ordinary_query(self):
        from otel_tracing.redact import sanitize_attributes, sanitize_url

        url = sanitize_url(
            "https://user:s3cret@api.line.me/v2/profile?access_token=line-token&page=2"
        )
        self.assertNotIn("s3cret", url)
        self.assertNotIn("line-token", url)
        self.assertIn("page=2", url)
        self.assertIn("REDACTED:REDACTED@", url)

        cleaned = sanitize_attributes(
            {
                "http.url": "https://api-v2.thaibulksms.com/credit?api_key=abc&pool=standard",
                "url.query": "token=abc&page=2",
                "http.request.header.authorization": ("Bearer line-token",),
                "http.request.header.content_type": ("application/json",),
                "db.statement": "SELECT %s",
                "db.statement.parameters": "('sql-param-secret-value',)",
                "http.request.body": "otp=123456",
                "http.request.body.size": 12,
                "bopp.partner.slug": "acme",
            }
        )
        blob = json.dumps(cleaned)
        self.assertNotIn("line-token", blob)
        self.assertNotIn("sql-param-secret-value", blob)
        self.assertNotIn("otp=123456", blob)
        self.assertNotIn("abc", blob)
        self.assertIn("page=2", blob)
        self.assertEqual(cleaned["db.statement"], "SELECT %s")
        self.assertEqual(cleaned["http.request.body.size"], 12)
        self.assertEqual(cleaned["bopp.partner.slug"], "acme")
        self.assertEqual(cleaned["http.request.header.content_type"], ("application/json",))
        self.assertNotIn("http.request.header.authorization", cleaned)
        self.assertNotIn("db.statement.parameters", cleaned)
        self.assertNotIn("http.request.body", cleaned)

    def test_server_span_uses_route_pattern_and_drops_query(self):
        from otel_tracing.redact import finalize_exported_span
        from otel_tracing.spans import match_route, normalize_route, preliminary_route

        self.assertEqual(
            normalize_route("/api/integrations/zortout/<string:token>"),
            "/api/integrations/zortout/<token>",
        )
        self.assertEqual(
            match_route(
                ["/api/partner/<string:slug>", "/api/partner/<string:slug>/user"],
                "/api/partner/acme/user",
            ),
            "/api/partner/<slug>/user",
        )
        self.assertEqual(preliminary_route("/api/partner/acme/user"), "/api/partner/<slug>/user")

        name, attrs = finalize_exported_span(
            "POST /api/integrations/zortout/raw-token-value?access_token=query-secret&page=2",
            {
                "http.method": "POST",
                "http.route": "/api/integrations/zortout/<token>",
                "http.target": "/api/integrations/zortout/raw-token-value?access_token=query-secret",
                "url.path": "/api/integrations/zortout/raw-token-value",
                "http.url": "http://odoo.local/api/integrations/zortout/raw-token-value?access_token=query-secret",
                "url.query": "access_token=query-secret&page=2",
                "bopp.partner.slug": "raw-slug-value",
                "http.request.header.authorization": "Bearer secret",
            },
            True,
        )
        blob = json.dumps({"name": name, "attrs": attrs})
        self.assertEqual(name, "POST /api/integrations/zortout/<token>")
        self.assertEqual(attrs["http.target"], "/api/integrations/zortout/<token>")
        self.assertEqual(attrs["url.path"], "/api/integrations/zortout/<token>")
        self.assertEqual(
            attrs["http.url"],
            "http://odoo.local/api/integrations/zortout/<token>",
        )
        self.assertNotIn("raw-token-value", blob)
        self.assertNotIn("query-secret", blob)
        self.assertNotIn("raw-slug-value", blob)
        self.assertNotIn("url.query", attrs)
        self.assertNotIn("bopp.partner.slug", attrs)
        self.assertNotIn("authorization", blob.lower())

    def test_aws_and_app_key_query_params(self):
        from otel_tracing.redact import sanitize_url

        url = sanitize_url(
            "/s3/object?X-Amz-Signature=sigvalue&X-Amz-Credential=cred&app_key=appsecret&ok=1"
        )
        self.assertNotIn("sigvalue", url)
        self.assertNotIn("cred", url)
        self.assertNotIn("appsecret", url)
        self.assertIn("ok=1", url)


class ServerWideConfigTests(unittest.TestCase):
    def test_appends_preserves_and_is_idempotent(self):
        from otel_tracing.server_wide import ensure_server_wide_module

        path = self._write("[options]\naddons_path = /mnt/extra-addons\nworkers = 2\n")
        ensure_server_wide_module(path)
        text = path.read_text()
        self.assertIn("server_wide_modules = base,web,otel_tracing\n", text)
        self.assertIn("workers = 2", text)
        ensure_server_wide_module(path)
        self.assertEqual(path.read_text().count("otel_tracing"), 1)

        path = self._write(
            "[options]\nserver_wide_modules = base,web,bus\n; server_wide_modules = commented\n"
        )
        ensure_server_wide_module(path)
        lines = [
            line
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith(";")
            and line.strip().startswith("server_wide_modules")
        ]
        self.assertEqual(lines, ["server_wide_modules = base,web,bus,otel_tracing"])

    def _write(self, text):
        path = Path(self.id().replace(".", "_") + ".conf")
        # Keep the file out of the repo tree.
        target = Path("/tmp") / path.name
        target.write_text(text)
        self.addCleanup(target.unlink, missing_ok=True)
        return target


class FlagTests(unittest.TestCase):
    def test_enabled_requires_true_and_an_endpoint(self):
        from otel_tracing.bootstrap import _enabled_state

        keys = (
            "OTEL_ENABLED",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_SDK_DISABLED",
        )
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            self.assertFalse(_enabled_state()[0])
            os.environ["OTEL_ENABLED"] = "1"
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://tempo:4318"
            self.assertFalse(_enabled_state()[0])
            os.environ["OTEL_ENABLED"] = "True"
            self.assertTrue(_enabled_state()[0])
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT")
            self.assertFalse(_enabled_state()[0])
            os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "http://tempo:4318/v1/traces"
            self.assertTrue(_enabled_state()[0])
            os.environ["OTEL_SDK_DISABLED"] = "true"
            self.assertFalse(_enabled_state()[0])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_fork_decision(self):
        from otel_tracing import bootstrap

        previous = {
            name: sys.modules.get(name) for name in ("odoo", "odoo.http", "odoo.tools")
        }
        try:
            _install_fake_odoo(workers=2, evented=False, app=object())
            self.assertTrue(bootstrap._will_fork())
            sys.modules["odoo"].evented = True
            self.assertFalse(bootstrap._will_fork())
            sys.modules["odoo"].evented = False
            sys.modules["odoo"].tools.config["workers"] = 0
            self.assertFalse(bootstrap._will_fork())
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class _Flags:
    def __init__(self, sampled):
        self.sampled = sampled


class _Context:
    def __init__(self, trace_id, valid=True, sampled=True):
        self.trace_id = trace_id
        self.is_valid = valid
        self.trace_flags = _Flags(sampled)


class _Span:
    def __init__(self, trace_id=0xAB, valid=True, sampled=True, recording=True, boom=False):
        self.context = _Context(trace_id, valid=valid, sampled=sampled)
        self.recording = recording
        self.boom = boom
        self.attributes = {}

    def get_span_context(self):
        if self.boom:
            raise RuntimeError("span context exploded")
        return self.context

    def is_recording(self):
        return self.recording

    def set_attribute(self, key, value):
        self.attributes[key] = value


class _MutableHeaders:
    """Stand-in for werkzeug's Headers: set/add/items, not a list."""

    def __init__(self):
        self._items = []

    def set(self, key, value):
        self._items = [(name, item) for name, item in self._items if name.lower() != key.lower()]
        self._items.append((key, value))

    def add(self, key, value):
        self._items.append((key, value))

    def get(self, key, default=None):
        for name, item in self._items:
            if name.lower() == key.lower():
                return item
        return default

    def items(self):
        return list(self._items)


class TraceHeaderTests(unittest.TestCase):
    def test_recording_and_sampled_contexts_set_a_32_hex_id(self):
        from otel_tracing import bootstrap

        recording = [("Content-Type", "text/plain")]
        span = _Span(trace_id=0x2A, recording=True, sampled=True)
        bootstrap._attach_trace_response_header(span, recording)
        self.assertEqual(
            _header_values(recording, "X-Trace-Id"),
            ["0000000000000000000000000000002a"],
        )

        sampled_only = []
        bootstrap._attach_trace_response_header(
            _Span(trace_id=0x2A, recording=False, sampled=True),
            sampled_only,
        )
        self.assertEqual(
            _header_values(sampled_only, "X-Trace-Id"),
            ["0000000000000000000000000000002a"],
        )
        self.assertEqual(_header_values(recording, "traceresponse"), [])

    def test_invalid_or_unsampled_context_is_skipped(self):
        from otel_tracing import bootstrap

        for span in (
            None,
            _Span(valid=False, recording=True, sampled=True),
            _Span(trace_id=0, valid=False, recording=False, sampled=False),
            _Span(trace_id=0x2A, valid=True, recording=False, sampled=False),
        ):
            headers = [("Content-Type", "text/plain")]
            bootstrap._attach_trace_response_header(span, headers)
            self.assertEqual(_header_values(headers, "X-Trace-Id"), [], span)
            self.assertEqual(headers, [("Content-Type", "text/plain")])

    def test_existing_header_is_not_duplicated_or_replaced(self):
        from otel_tracing import bootstrap

        headers = [("x-trace-id", "already-set"), ("Content-Type", "application/json")]
        body_owner = headers
        bootstrap._attach_trace_response_header(_Span(), headers)
        bootstrap._attach_trace_response_header(_Span(trace_id=0xFF), headers)
        self.assertIs(headers, body_owner)
        self.assertEqual(_header_values(headers, "X-Trace-Id"), ["already-set"])

    def test_cors_expose_header_is_merged_and_only_when_origin_is_set(self):
        from otel_tracing import bootstrap

        headers = [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Expose-Headers", "Content-Type, X-Custom"),
            ("Content-Type", "application/json"),
        ]
        bootstrap._attach_trace_response_header(_Span(), headers)
        expose = _header_values(headers, "Access-Control-Expose-Headers")
        self.assertEqual(expose, ["Content-Type, X-Custom, X-Trace-Id"])
        bootstrap._attach_trace_response_header(_Span(), headers)
        self.assertEqual(_header_values(headers, "Access-Control-Expose-Headers"), expose)
        self.assertEqual(len(_header_values(headers, "X-Trace-Id")), 1)

        fresh = [("Access-Control-Allow-Origin", "https://app.example")]
        bootstrap._attach_trace_response_header(_Span(), fresh)
        self.assertEqual(_header_values(fresh, "Access-Control-Expose-Headers"), ["X-Trace-Id"])

        plain = [("Content-Type", "text/plain")]
        bootstrap._attach_trace_response_header(_Span(), plain)
        self.assertEqual(_header_values(plain, "Access-Control-Expose-Headers"), [])

        multi = _MutableHeaders()
        multi.add("Access-Control-Allow-Origin", "*")
        multi.add("Access-Control-Expose-Headers", "Content-Disposition")
        multi.add("Access-Control-Expose-Headers", "X-Custom")
        bootstrap._expose_trace_id(multi)
        self.assertEqual(
            multi.get("Access-Control-Expose-Headers"),
            "Content-Disposition, X-Custom, X-Trace-Id",
        )
        self.assertEqual(
            sum(
                1
                for key, _value in multi.items()
                if key.lower() == "access-control-expose-headers"
            ),
            1,
        )

    def test_response_hook_sets_status_and_survives_a_bad_status(self):
        from otel_tracing import bootstrap

        span = _Span()
        headers = []
        bootstrap._wsgi_response_hook(span, {}, "404 NOT FOUND", headers)
        self.assertEqual(span.attributes["http.response.status_code"], 404)
        self.assertEqual(len(_header_values(headers, "X-Trace-Id")), 1)

        broken = _Span()
        broken_headers = [("Content-Type", "text/plain")]
        bootstrap._wsgi_response_hook(broken, {}, "nope", broken_headers)
        self.assertNotIn("http.response.status_code", broken.attributes)
        self.assertEqual(len(_header_values(broken_headers, "X-Trace-Id")), 1)

    def test_header_failure_is_logged_once_and_the_response_headers_stay(self):
        import logging

        from otel_tracing import bootstrap

        bootstrap._warned_once.discard("trace-header")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("otel_tracing.bootstrap")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        self.addCleanup(logger.removeHandler, handler)
        headers = [("Content-Type", "text/plain")]
        span = _Span(boom=True)
        bootstrap._wsgi_response_hook(span, {}, "500 Internal Server Error", headers)
        bootstrap._wsgi_response_hook(span, {}, "500 Internal Server Error", headers)
        self.assertEqual(headers, [("Content-Type", "text/plain")])
        self.assertEqual(stream.getvalue().count("failed to add X-Trace-Id"), 1)
        self.assertIn("WARNING", stream.getvalue())

    def test_pre_dispatch_merges_expose_header_and_fails_open(self):
        import logging
        import types

        from otel_tracing import bootstrap

        http = types.ModuleType("odoo.http")

        class Dispatcher:
            def pre_dispatch(self, rule, args):
                headers = self.request.future_response.headers
                if getattr(rule, "cors", False):
                    headers.set("Access-Control-Allow-Origin", "*")
                    existing = getattr(rule, "expose", None)
                    if existing:
                        headers.set("Access-Control-Expose-Headers", existing)
                if getattr(rule, "explode", False):
                    raise RuntimeError("options abort")
                return "dispatched"

        http.Dispatcher = Dispatcher
        previous = {
            name: sys.modules.get(name) for name in ("odoo", "odoo.http")
        }
        odoo = types.ModuleType("odoo")
        odoo.http = http
        sys.modules["odoo"] = odoo
        sys.modules["odoo.http"] = http
        bootstrap._warned_once.discard("cors-expose")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("otel_tracing.bootstrap")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            bootstrap._install_cors_expose()
            bootstrap._install_cors_expose()
            self.assertTrue(Dispatcher.pre_dispatch._bopp_otel)

            headers = _MutableHeaders()
            dispatcher = Dispatcher()
            rule = types.SimpleNamespace(cors=True, expose="Content-Disposition")
            dispatcher.request = types.SimpleNamespace(
                future_response=types.SimpleNamespace(headers=headers)
            )
            self.assertEqual(dispatcher.pre_dispatch(rule, {}), "dispatched")
            self.assertEqual(
                headers.get("Access-Control-Expose-Headers"),
                "Content-Disposition, X-Trace-Id",
            )
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
            dispatcher.pre_dispatch(rule, {})
            self.assertEqual(headers.get("Access-Control-Expose-Headers").count("X-Trace-Id"), 1)

            aborted = _MutableHeaders()
            dispatcher.request = types.SimpleNamespace(
                future_response=types.SimpleNamespace(headers=aborted)
            )
            with self.assertRaises(RuntimeError):
                dispatcher.pre_dispatch(types.SimpleNamespace(cors=True, explode=True), {})
            self.assertEqual(aborted.get("Access-Control-Expose-Headers"), "X-Trace-Id")

            quiet = _MutableHeaders()
            dispatcher.request = types.SimpleNamespace(
                future_response=types.SimpleNamespace(headers=quiet)
            )
            self.assertEqual(dispatcher.pre_dispatch(types.SimpleNamespace(cors=False), {}), "dispatched")
            self.assertIsNone(quiet.get("Access-Control-Expose-Headers"))

            class BoomHeaders:
                def set(self, key, value):
                    return None

                def items(self):
                    raise RuntimeError("header store exploded")

            dispatcher.request = types.SimpleNamespace(
                future_response=types.SimpleNamespace(headers=BoomHeaders())
            )
            self.assertEqual(
                dispatcher.pre_dispatch(types.SimpleNamespace(cors=True), {}),
                "dispatched",
            )
            dispatcher.pre_dispatch(types.SimpleNamespace(cors=True), {})
            self.assertEqual(stream.getvalue().count("failed to expose X-Trace-Id"), 1)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class ScenarioTests(unittest.TestCase):
    def test_disabled_is_a_noop(self):
        proc = run_scenario("disabled", _clean_env())
        self.assertIn("disabled-ok", proc.stdout)

    def test_enabled_waterfall_redaction_and_logs(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_SERVICE_NAME="bopp-odoo-test",
            OTEL_TRACES_SAMPLER="always_on",
        )
        if os.environ.get("OTEL_TEST_PG_DSN"):
            env["OTEL_TEST_PG_DSN"] = os.environ["OTEL_TEST_PG_DSN"]
        proc = run_scenario("enabled", env)
        self.assertIn("enabled-ok", proc.stdout)

    def test_unreachable_exporter_does_not_break_the_request(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_SERVICE_NAME="bopp-odoo",
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_EXPORTER_OTLP_TIMEOUT="1",
            OTEL_BSP_EXPORT_TIMEOUT="1000",
        )
        proc = run_scenario("unreachable", env, timeout=30)
        self.assertIn("unreachable-ok", proc.stdout)

    def test_provider_is_created_in_the_forked_worker(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_SERVICE_NAME="bopp-odoo",
            OTEL_TRACES_SAMPLER="always_on",
        )
        proc = run_scenario("fork", env)
        self.assertIn("fork-ok", proc.stdout)

    def test_gevent_process_initializes_without_forking(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_TRACES_SAMPLER="always_on",
        )
        proc = run_scenario("gevent", env)
        self.assertIn("gevent-ok", proc.stdout)

    def test_trace_id_header_on_success_error_redirect_and_api(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_SERVICE_NAME="bopp-odoo",
            OTEL_TRACES_SAMPLER="always_on",
        )
        proc = run_scenario("trace-header", env)
        self.assertIn("trace-header-ok", proc.stdout)

    def test_sampler_arg_is_honored(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_TRACES_SAMPLER="traceidratio",
            OTEL_TRACES_SAMPLER_ARG="0",
        )
        proc = run_scenario("sampler-off", env)
        self.assertIn("sampler-off-ok", proc.stdout)

    def test_fork_after_threadpool_import_does_not_hang(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_SERVICE_NAME="bopp-odoo",
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_INIT_TIMEOUT="5",
        )
        proc = run_scenario("fork-threadpool", env, timeout=20)
        self.assertIn("fork-threadpool-ok", proc.stdout)

    def test_init_timeout_still_serves(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_INIT_TIMEOUT="0.3",
        )
        proc = run_scenario("init-timeout", env, timeout=15)
        self.assertIn("init-timeout-ok", proc.stdout)

    def test_init_error_still_serves(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
        )
        proc = run_scenario("init-error", env, timeout=15)
        self.assertIn("init-error-ok", proc.stdout)

    def test_method_spans_nest_under_the_parent_and_fail_open(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_ODOO_METHOD_SPANS="true",
        )
        proc = run_scenario("method-spans", env, timeout=20)
        self.assertIn("method-spans-ok", proc.stdout)

    def test_method_spans_can_be_disabled(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_ODOO_METHOD_SPANS="false",
        )
        proc = run_scenario("method-spans-off", env, timeout=20)
        self.assertIn("method-spans-off-ok", proc.stdout)

    def test_short_method_spans_are_not_exported(self):
        from types import SimpleNamespace

        from otel_tracing.bootstrap import _MethodDurationFilter

        class Sink:
            def __init__(self):
                self.spans = []

            def on_end(self, span):
                self.spans.append(span)

            def on_start(self, span, parent_context=None):
                return None

            def shutdown(self):
                return None

            def force_flush(self, timeout_millis=30000):
                return True

        sink = Sink()
        filt = _MethodDurationFilter(sink, min_ns=5_000_000)
        short = SimpleNamespace(
            start_time=0,
            end_time=1_000_000,
            attributes={"bopp.span.group": "method"},
        )
        kept = SimpleNamespace(
            start_time=0,
            end_time=1_000_000,
            attributes={"bopp.span.group": "controller"},
        )
        slow = SimpleNamespace(
            start_time=0,
            end_time=6_000_000,
            attributes={"bopp.span.group": "method"},
        )
        filt.on_end(short)
        filt.on_end(kept)
        filt.on_end(slow)
        self.assertEqual(sink.spans, [kept, slow])

    def test_grpc_exporter_is_selected_from_the_protocol(self):
        env = _clean_env(
            OTEL_ENABLED="true",
            OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
            OTEL_EXPORTER_OTLP_PROTOCOL="grpc",
            OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="http/protobuf",
        )
        proc = run_scenario("protocol", env)
        self.assertIn("protocol-ok", proc.stdout)


def _install_fake_odoo(workers, evented, app):
    import types

    odoo = types.ModuleType("odoo")
    odoo.evented = evented
    http = types.ModuleType("odoo.http")
    http.root = app
    tools = types.ModuleType("odoo.tools")
    tools.config = {"workers": workers}
    service = types.ModuleType("odoo.service")
    server = types.ModuleType("odoo.service.server")

    class Worker:
        def start(self):
            self.started = True

    server.Worker = Worker
    odoo.http = http
    odoo.tools = tools
    odoo.service = service
    sys.modules["odoo"] = odoo
    sys.modules["odoo.http"] = http
    sys.modules["odoo.tools"] = tools
    sys.modules["odoo.service"] = service
    sys.modules["odoo.service.server"] = server
    return odoo


def _header_values(headers, name):
    return [value for key, value in headers if str(key).lower() == name.lower()]


def _invoke(app, path, method="GET", query=""):
    status = {}

    def start_response(code, headers, exc_info=None):
        status["code"] = code
        status["headers"] = [(str(key), str(value)) for key, value in headers]
        return None

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8069",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "HTTP_HOST": "localhost:8069",
    }
    body = app(environ, start_response)
    payload = b"".join(body)
    close = getattr(body, "close", None)
    if close:
        close()
    return status.get("code", ""), payload, status.get("headers", [])


def _call(app, path, method="GET", query=""):
    code, payload, _headers = _invoke(app, path, method=method, query=query)
    return code, payload


def _memory_exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


def _use_memory_exporter(bootstrap):
    exporter = _memory_exporter()

    def _build():
        return exporter

    bootstrap._build_exporter = _build
    return exporter


def _span_blob(spans):
    parts = []
    for span in spans:
        parts.append(span.name)
        for key, value in (span.attributes or {}).items():
            parts.append(str(key))
            parts.append(str(value))
    return "\n".join(parts)


class _App:
    session_store = "still-here"

    def __init__(self, work=None, status="200 OK", body=b"ok", response_headers=None):
        self.work = work
        self.status = status
        self.body = body
        self.response_headers = response_headers

    def __call__(self, environ, start_response):
        if self.work:
            self.work()
        headers = [("Content-Type", "text/plain")]
        if self.response_headers is not None:
            headers = [tuple(item) for item in self.response_headers]
        start_response(self.status, headers)
        body = self.body if isinstance(self.body, bytes) else str(self.body).encode()
        return [body]


def scenario_disabled():
    import logging

    from otel_tracing.bootstrap import setup

    app = _App()
    _install_fake_odoo(workers=0, evented=False, app=app)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    setup()
    assert "opentelemetry.sdk.trace" not in sys.modules, sys.modules.keys()
    assert not getattr(type(app), "_bopp_otel_wrapped", False)
    code, payload, headers = _invoke(app, "/api/partner/acme/user")
    assert code.startswith("200"), code
    assert payload == b"ok"
    assert _header_values(headers, "X-Trace-Id") == []
    assert _header_values(headers, "traceresponse") == []
    assert app.session_store == "still-here"
    logging.getLogger("disabled-check").info("hello")
    assert "otel_trace_id" not in (handler.formatter._fmt or "")
    assert "trace_id=" not in stream.getvalue()
    print("disabled-ok")


def scenario_enabled():
    import logging
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import requests

    from otel_tracing import bootstrap

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    dsn = os.environ.get("OTEL_TEST_PG_DSN")

    def work():
        logging.getLogger("otel.scenario").info("inside-request")
        requests.get(
            f"http://127.0.0.1:{port}/v2/profile?access_token=query-token-secret&page=2",
            headers={"Authorization": "Bearer line-access-token-super-secret"},
            timeout=5,
        )
        if dsn:
            import psycopg2

            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()
                cur.execute("SELECT %s::text", ("sql-param-secret-value",))
                cur.fetchone()
            finally:
                conn.close()

    app = _App(work=work)
    _install_fake_odoo(workers=0, evented=False, app=app)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)

    class _OdooLike(logging.Formatter):
        def format(self, record):
            record.pid = os.getpid()
            record.dbname = "?"
            if not hasattr(record, "perf_info"):
                record.perf_info = "- - -"
            return super().format(record)

    handler.setFormatter(
        _OdooLike(
            "%(asctime)s %(pid)s %(levelname)s %(dbname)s %(name)s: %(message)s %(perf_info)s"
        )
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()
    assert app.session_store == "still-here"
    code, _payload = _call(
        app,
        "/api/partner/acme/user",
        query="access_token=server-query-secret&page=3",
    )
    assert code.startswith("200"), code
    from opentelemetry import trace

    trace.get_tracer_provider().force_flush()
    spans = list(exporter.get_finished_spans())
    blob = _span_blob(spans)
    assert spans, "expected spans"
    assert "line-access-token-super-secret" not in blob, blob
    assert "query-token-secret" not in blob, blob
    assert "sql-param-secret-value" not in blob, blob
    assert "page=2" in blob, blob
    for span in spans:
        for key in span.attributes or {}:
            lowered = key.lower()
            assert "authorization" not in lowered, key
            assert "cookie" not in lowered, key
            assert not lowered.startswith("db.statement.parameters"), key
    server_spans = [span for span in spans if span.kind == trace.SpanKind.SERVER]
    assert len(server_spans) == 1, [(span.name, span.kind) for span in spans]
    server_span = server_spans[0]
    assert server_span.name == "GET /api/partner/<slug>/user", server_span.name
    assert "bopp.partner.slug" not in dict(server_span.attributes)
    assert dict(server_span.attributes).get("http.route") == "/api/partner/<slug>/user"
    assert dict(server_span.attributes).get("http.target") == "/api/partner/<slug>/user"
    assert "acme" not in blob, blob
    assert "server-query-secret" not in blob, blob
    assert dict(server_span.attributes).get("http.response.status_code") == 200
    trace_id = server_span.context.trace_id
    for span in spans:
        assert span.context.trace_id == trace_id, span.name
    client_spans = [span for span in spans if span.kind == trace.SpanKind.CLIENT]
    assert client_spans, blob
    sql_spans = [
        span
        for span in spans
        if "db.statement" in (span.attributes or {})
        or "db.query.text" in (span.attributes or {})
    ]
    if dsn:
        assert sql_spans, blob
        assert sql_spans[0].parent is not None
        assert sql_spans[0].parent.span_id == server_span.context.span_id
        statement = dict(sql_spans[0].attributes).get("db.statement") or dict(
            sql_spans[0].attributes
        ).get("db.query.text")
        assert "SELECT" in statement
        assert "sql-param-secret-value" not in statement
    else:
        assert not sql_spans
    http_children = [
        span
        for span in client_spans
        if span.parent is not None and span.parent.span_id == server_span.context.span_id
    ]
    assert http_children, [(span.name, span.parent) for span in client_spans]
    logged = stream.getvalue()
    assert f"trace_id={format(trace_id, '032x')}" in logged, logged
    assert f"span_id={format(server_span.context.span_id, '016x')}" in logged, logged
    service = trace.get_tracer_provider().resource.attributes["service.name"]
    assert service == "bopp-odoo-test", service
    server.shutdown()
    print("enabled-ok")


def scenario_unreachable():
    import time

    from otel_tracing.bootstrap import setup

    app = _App()
    _install_fake_odoo(workers=0, evented=False, app=app)
    setup()
    started = time.perf_counter()
    code, payload = _call(app, "/web/health")
    elapsed = time.perf_counter() - started
    assert code.startswith("200"), code
    assert payload == b"ok"
    assert elapsed < 1.0, elapsed
    started = time.perf_counter()
    code, _payload = _call(app, "/api/partner/acme/user")
    elapsed = time.perf_counter() - started
    assert code.startswith("200"), code
    assert elapsed < 1.0, elapsed
    from opentelemetry import trace

    # Flush hits the closed port. It must not raise into the caller.
    trace.get_tracer_provider().force_flush()
    print("unreachable-ok")


def scenario_fork():
    import os

    from otel_tracing import bootstrap

    app = _App()
    _install_fake_odoo(workers=2, evented=False, app=app)
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()
    assert bootstrap._provider_pid is None
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            assert bootstrap._provider_pid is None
            from odoo.service.server import Worker

            worker = Worker()
            worker.start()
            assert worker.started is True
            assert bootstrap._provider_pid == os.getpid(), bootstrap._provider_pid
            code, _payload = _call(app, "/api/partner/acme/user")
            assert code.startswith("200"), code
            from opentelemetry import trace

            trace.get_tracer_provider().force_flush()
            spans = list(exporter.get_finished_spans())
            server_spans = [span for span in spans if span.kind == trace.SpanKind.SERVER]
            assert len(server_spans) == 1, [span.name for span in spans]
            blob = _span_blob(spans)
            payload = {
                "pid": os.getpid(),
                "name": server_spans[0].name,
                "route": dict(server_spans[0].attributes).get("http.route"),
                "target": dict(server_spans[0].attributes).get("http.target"),
                "count": len(spans),
                "blob": blob,
            }
            os.write(write_fd, json.dumps(payload).encode())
            os.close(write_fd)
        except Exception:
            import traceback

            os.write(write_fd, b"ERROR " + traceback.format_exc().encode())
            os.close(write_fd)
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    _pid_done, status = os.waitpid(pid, 0)
    chunks = []
    while True:
        piece = os.read(read_fd, 65536)
        if not piece:
            break
        chunks.append(piece)
    os.close(read_fd)
    raw = b"".join(chunks).decode()
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, raw
    assert bootstrap._provider_pid is None
    # The child's in-memory exporter is not this process's object.
    assert exporter.get_finished_spans() == ()
    payload = json.loads(raw)
    assert payload["pid"] != os.getpid()
    assert payload["name"] == "GET /api/partner/<slug>/user"
    assert payload["route"] == "/api/partner/<slug>/user"
    assert payload["target"] == "/api/partner/<slug>/user"
    assert "acme" not in payload["blob"]
    assert payload["count"] >= 1
    print("fork-ok")


def scenario_fork_threadpool():
    """Registry preload imports concurrent.futures.thread after post_load.

    That is what crm_custom does via boto3. Creating the SDK from an
    at-fork hook then deadlocks ThreadPoolExecutor.submit. Worker.start
    runs after the lock has been reinitialized and must return.
    """
    import time

    from otel_tracing import bootstrap

    app = _App()
    _install_fake_odoo(workers=3, evented=False, app=app)
    bootstrap.setup()
    assert bootstrap._provider_pid is None
    import concurrent.futures.thread  # noqa: F401  (after the hook, like preload)

    import select

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            from odoo.service.server import Worker

            started = time.perf_counter()
            Worker().start()
            elapsed = time.perf_counter() - started
            assert elapsed < 5, elapsed
            assert bootstrap._provider_pid == os.getpid(), bootstrap._provider_pid
            code, _payload = _call(app, "/web/health")
            assert code.startswith("200"), code
            os.write(write_fd, b"ok")
        except Exception:
            import traceback

            os.write(write_fd, b"ERROR " + traceback.format_exc().encode())
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    deadline = time.perf_counter() + 12
    raw = b""
    while time.perf_counter() < deadline and not raw:
        ready, _, _ = select.select([read_fd], [], [], 0.2)
        if ready:
            try:
                raw = os.read(read_fd, 65536)
            except BlockingIOError:
                raw = b""
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if waited and not raw:
            try:
                raw = os.read(read_fd, 65536)
            except BlockingIOError:
                raw = b""
            break
    else:
        if not raw:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            raise AssertionError("child hung after fork with thread pool preloaded")
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    os.close(read_fd)
    text = raw.decode()
    assert text == "ok", text
    print("fork-threadpool-ok")


def scenario_init_timeout():
    import time

    from otel_tracing import bootstrap

    def _hang():
        time.sleep(30)

    bootstrap._init_provider = _hang
    app = _App()
    _install_fake_odoo(workers=3, evented=False, app=app)
    bootstrap.setup()
    from odoo.service.server import Worker

    started = time.perf_counter()
    worker = Worker()
    worker.start()
    elapsed = time.perf_counter() - started
    assert worker.started is True
    assert elapsed < 2.0, elapsed
    code, _payload = _call(app, "/web/health")
    assert code.startswith("200"), code
    print("init-timeout-ok")


def scenario_init_error():
    from otel_tracing import bootstrap

    def _boom():
        raise RuntimeError("collector init exploded")

    bootstrap._init_provider = _boom
    app = _App()
    _install_fake_odoo(workers=3, evented=False, app=app)
    bootstrap.setup()
    from odoo.service.server import Worker

    worker = Worker()
    worker.start()
    assert worker.started is True
    assert bootstrap._provider_pid is None
    code, _payload = _call(app, "/web/health")
    assert code.startswith("200"), code
    print("init-error-ok")


def scenario_gevent():
    from otel_tracing import bootstrap

    app = _App()
    _install_fake_odoo(workers=4, evented=True, app=app)
    _use_memory_exporter(bootstrap)
    bootstrap.setup()
    assert bootstrap._provider_pid == os.getpid()
    from opentelemetry import trace

    service = trace.get_tracer_provider().resource.attributes["service.name"]
    assert service == "bopp-odoo", service
    code, _payload = _call(app, "/longpolling/poll")
    assert code.startswith("200"), code
    print("gevent-ok")


def scenario_sampler_off():
    from otel_tracing import bootstrap

    app = _App()
    _install_fake_odoo(workers=0, evented=False, app=app)
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()
    code, payload, headers = _invoke(app, "/web/health")
    assert code.startswith("200"), code
    assert payload == b"ok"
    assert _header_values(headers, "X-Trace-Id") == [], headers
    assert _header_values(headers, "traceresponse") == []
    from opentelemetry import trace

    trace.get_tracer_provider().force_flush()
    assert list(exporter.get_finished_spans()) == []
    print("sampler-off-ok")


def scenario_method_spans():
    from opentelemetry import trace

    from otel_tracing import bootstrap, spans

    class Partner:
        _name = "res.partner"

        def __len__(self):
            return 4

        def write(self, vals):
            return True

        def search(self, domain, limit=None):
            return ["a", "b"]

    Partner.write._api = "model"

    class Controller:
        def receive_webhook(self, token, **kwargs):
            self.token = token
            Partner().search([])
            return "ok"

    controller = Controller()

    def endpoint(**kwargs):
        return controller.receive_webhook(**kwargs)

    endpoint.__self__ = controller
    endpoint.__name__ = "receive_webhook"
    endpoint.routing = {"routes": ["/api/integrations/zortout/<string:token>"]}

    class _HttpRequest:
        path = "/api/integrations/zortout/raw-token-value"

    class Request:
        def __init__(self):
            self.httprequest = _HttpRequest()

    class HttpDispatcher:
        def __init__(self, request):
            self.request = request

        def dispatch(self, endpoint, args):
            return endpoint(**(args or {}))

    class Cron:
        def _callback(self, cron_name, server_action_id):
            self.called = (cron_name, server_action_id)
            return "ran"

    spans.patch_base_methods(Partner)
    assert Partner.write._api == "model"
    spans.wrap_cron(Cron)

    def work():
        dispatcher = HttpDispatcher(Request())
        result = dispatcher.dispatch(endpoint, {"token": "raw-token-value"})
        assert result == "ok", result
        assert Partner().write({"name": "x"}) is True

    app = _App(work=work)
    _install_fake_odoo(workers=0, evented=False, app=app)
    import odoo.http as http

    http.Request = Request
    http.HttpDispatcher = HttpDispatcher
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()
    code, _payload = _call(app, "/api/integrations/zortout/raw-token-value")
    assert code.startswith("200"), code
    assert controller.token == "raw-token-value"

    cron = Cron()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("manual"):
        assert cron._callback("Nightly sync", 7) == "ran"
    assert cron.called == ("Nightly sync", 7)

    original_tracer = spans._tracer

    def _boom():
        raise RuntimeError("tracer exploded")

    spans._tracer = _boom
    with tracer.start_as_current_span("broken-tracer"):
        assert Partner().write({"name": "y"}) is True
    spans._tracer = original_tracer

    trace.get_tracer_provider().force_flush()
    finished = list(exporter.get_finished_spans())
    blob = _span_blob(finished)
    names = [span.name for span in finished]
    assert "res.partner.search" in names, names
    assert "res.partner.write" in names, names
    assert "controller Controller.receive_webhook" in names, names
    assert "cron Nightly sync" in names, names
    assert "raw-token-value" not in blob, blob
    server_spans = [span for span in finished if span.kind == trace.SpanKind.SERVER]
    assert len(server_spans) == 1, names
    assert server_spans[0].name == "GET /api/integrations/zortout/<token>", server_spans[0].name
    assert dict(server_spans[0].attributes).get("http.route") == "/api/integrations/zortout/<token>"
    search_span = next(span for span in finished if span.name == "res.partner.search")
    controller_span = next(
        span for span in finished if span.name == "controller Controller.receive_webhook"
    )
    assert search_span.parent.span_id == controller_span.context.span_id
    assert controller_span.parent.span_id == server_spans[0].context.span_id
    assert dict(controller_span.attributes).get("code.function") == "receive_webhook"
    assert "Controller" in dict(controller_span.attributes).get("code.namespace", "")
    write_span = next(span for span in finished if span.name == "res.partner.write")
    assert dict(write_span.attributes).get("odoo.record_count") == 4
    assert dict(search_span.attributes).get("odoo.record_count") == 2
    print("method-spans-ok")


def scenario_method_spans_off():
    from opentelemetry import trace

    from otel_tracing import bootstrap, spans

    class Partner:
        _name = "res.partner"

        def __len__(self):
            return 1

        def write(self, vals):
            return "off"

    spans.patch_base_methods(Partner)
    app = _App()
    _install_fake_odoo(workers=0, evented=False, app=app)
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent"):
        assert Partner().write({}) == "off"
    trace.get_tracer_provider().force_flush()
    names = [span.name for span in exporter.get_finished_spans()]
    assert "res.partner.write" not in names, names
    assert "parent" in names, names
    print("method-spans-off-ok")


def scenario_trace_header():
    import logging

    from opentelemetry import trace

    from otel_tracing import bootstrap

    app = _App()
    _install_fake_odoo(workers=0, evented=False, app=app)
    exporter = _use_memory_exporter(bootstrap)
    bootstrap.setup()

    def check(path, status, body, response_headers=None, expect_trace=True):
        app.status = status
        app.body = body
        app.response_headers = response_headers
        code, payload, headers = _invoke(app, path)
        assert code == status, code
        assert payload == body, payload
        assert _header_values(headers, "traceresponse") == [], headers
        trace.get_tracer_provider().force_flush()
        server_spans = [
            span
            for span in exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
        assert server_spans, "expected a server span"
        server = server_spans[-1]
        status_code = int(status.split(" ", 1)[0])
        assert dict(server.attributes).get("http.response.status_code") == status_code
        values = _header_values(headers, "X-Trace-Id")
        if expect_trace:
            assert len(values) == 1, headers
            assert values[0] == format(server.context.trace_id, "032x"), headers
            assert len(values[0]) == 32 and all(char in "0123456789abcdef" for char in values[0])
        return headers

    check("/web/health", "200 OK", b"ok")
    check("/missing", "404 NOT FOUND", b"missing page")
    check("/boom", "500 Internal Server Error", b"err")
    redirect = check(
        "/old",
        "302 FOUND",
        b"",
        response_headers=[("Location", "/web/login"), ("Content-Type", "text/html")],
    )
    assert _header_values(redirect, "Location") == ["/web/login"], redirect
    check(
        "/api/partner/acme/user",
        "200 OK",
        b'{"ok":true}',
        response_headers=[("Content-Type", "application/json")],
    )
    duplicate = check(
        "/api/duplicate",
        "200 OK",
        b"{}",
        response_headers=[
            ("Content-Type", "application/json"),
            ("X-Trace-Id", "already-set"),
        ],
        expect_trace=False,
    )
    assert _header_values(duplicate, "X-Trace-Id") == ["already-set"], duplicate
    cors = check(
        "/api/portal/me",
        "200 OK",
        b'{"id":1}',
        response_headers=[
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Expose-Headers", "Content-Type, X-Custom"),
            ("Content-Type", "application/json"),
        ],
    )
    assert _header_values(cors, "Access-Control-Expose-Headers") == [
        "Content-Type, X-Custom, X-Trace-Id"
    ], cors
    plain = check("/web/session/logout", "200 OK", b"bye")
    assert _header_values(plain, "Access-Control-Expose-Headers") == []

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("otel_tracing.bootstrap")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("header exploded")

    bootstrap._attach_trace_response_header = _boom
    bootstrap._warned_once.discard("trace-header")
    app.status = "200 OK"
    app.body = b"still-ok"
    app.response_headers = None
    code, payload, _headers = _invoke(app, "/web/health")
    code_again, payload_again, _headers_again = _invoke(app, "/web/health")
    assert code == "200 OK" and payload == b"still-ok", (code, payload)
    assert code_again == "200 OK" and payload_again == b"still-ok"
    assert stream.getvalue().count("failed to add X-Trace-Id") == 1, stream.getvalue()
    print("trace-header-ok")


def scenario_protocol():
    from otel_tracing.bootstrap import _build_exporter, _protocol

    # Traces-specific protocol overrides the general one.
    assert _protocol() == "http/protobuf", _protocol()
    os.environ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] = "grpc"
    assert _protocol() == "grpc"
    exporter = _build_exporter()
    assert type(exporter).__name__ == "OTLPSpanExporter"
    assert "otlp.proto.grpc" in type(exporter).__module__
    print("protocol-ok")


SCENARIOS = {
    "disabled": scenario_disabled,
    "enabled": scenario_enabled,
    "unreachable": scenario_unreachable,
    "fork": scenario_fork,
    "fork-threadpool": scenario_fork_threadpool,
    "init-timeout": scenario_init_timeout,
    "init-error": scenario_init_error,
    "method-spans": scenario_method_spans,
    "method-spans-off": scenario_method_spans_off,
    "gevent": scenario_gevent,
    "sampler-off": scenario_sampler_off,
    "trace-header": scenario_trace_header,
    "protocol": scenario_protocol,
}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        SCENARIOS[sys.argv[2]]()
    else:
        unittest.main()
