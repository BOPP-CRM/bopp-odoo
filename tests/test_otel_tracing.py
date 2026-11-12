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
    odoo.http = http
    odoo.tools = tools
    sys.modules["odoo"] = odoo
    sys.modules["odoo.http"] = http
    sys.modules["odoo.tools"] = tools
    return odoo


def _call(app, path, method="GET"):
    status = {}

    def start_response(code, headers, exc_info=None):
        status["code"] = code
        return None

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
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
    return status.get("code", ""), payload


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

    def __init__(self, work=None):
        self.work = work

    def __call__(self, environ, start_response):
        if self.work:
            self.work()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


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
    code, payload = _call(app, "/api/partner/acme/user")
    assert code.startswith("200"), code
    assert payload == b"ok"
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
    code, _payload = _call(app, "/api/partner/acme/user")
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
    assert server_span.name == "GET /api/partner/{slug}/user", server_span.name
    assert dict(server_span.attributes).get("bopp.partner.slug") == "acme"
    assert dict(server_span.attributes).get("http.route") == "/api/partner/{slug}/user"
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
            assert bootstrap._provider_pid == os.getpid(), bootstrap._provider_pid
            code, _payload = _call(app, "/api/partner/acme/user")
            assert code.startswith("200"), code
            from opentelemetry import trace

            trace.get_tracer_provider().force_flush()
            spans = list(exporter.get_finished_spans())
            server_spans = [span for span in spans if span.kind == trace.SpanKind.SERVER]
            assert len(server_spans) == 1, [span.name for span in spans]
            payload = {
                "pid": os.getpid(),
                "name": server_spans[0].name,
                "slug": dict(server_spans[0].attributes).get("bopp.partner.slug"),
                "count": len(spans),
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
    assert payload["name"] == "GET /api/partner/{slug}/user"
    assert payload["slug"] == "acme"
    assert payload["count"] >= 1
    print("fork-ok")


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
    code, _payload = _call(app, "/web/health")
    assert code.startswith("200"), code
    from opentelemetry import trace

    trace.get_tracer_provider().force_flush()
    assert list(exporter.get_finished_spans()) == []
    print("sampler-off-ok")


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
    "gevent": scenario_gevent,
    "sampler-off": scenario_sampler_off,
    "protocol": scenario_protocol,
}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        SCENARIOS[sys.argv[2]]()
    else:
        unittest.main()
