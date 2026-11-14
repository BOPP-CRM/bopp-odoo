"""Boot real Odoo 18 in prefork mode with tracing on.

Requires Postgres, an Odoo 18 checkout (ODOO_SRC, default /tmp/odoo), and the
Python dependencies of Odoo plus crm_custom (boto3, Pillow). Loading
crm_custom imports boto3, which imports concurrent.futures.thread, during
registry preload — the sequence that deadlocked prefork workers.
"""

import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ODOO_SRC = Path(os.environ.get("ODOO_SRC", "/tmp/odoo"))
CONF = Path(os.environ.get("OTEL_ODOO_CONF", "/tmp/otel-odoo.conf"))
HTTP_PORT = int(os.environ.get("OTEL_TEST_HTTP_PORT", "18069"))
DB_NAME = os.environ.get("OTEL_TEST_DB", "otel_prefork")

_SPANS = []
_SPAN_LOCK = threading.Lock()


class _Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        encoding = (self.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            import gzip

            body = gzip.decompress(body)
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        with _SPAN_LOCK:
            _SPANS.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def _server_instance_ids():
    ids = set()
    with _SPAN_LOCK:
        batches = list(_SPANS)
    for batch in batches:
        for resource_spans in batch.resource_spans:
            attrs = {}
            for attribute in resource_spans.resource.attributes:
                value = attribute.value
                attrs[attribute.key] = value.string_value or str(value.int_value or "")
            instance = attrs.get("service.instance.id") or ""
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    # SPAN_KIND_SERVER == 2
                    if span.kind == 2 and instance:
                        ids.add(instance)
    return ids


def _run(cmd, env, log_path, timeout):
    import subprocess

    log = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ODOO_SRC),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        raise
    finally:
        log.close()
    return code


def _pythonpath():
    return os.pathsep.join(
        [
            str(ODOO_SRC),
            str(ROOT / "src"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )


def _base_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = _pythonpath()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in list(env):
        if key.startswith("OTEL_"):
            del env[key]
    return env


FIXTURE_ADDON = Path("/tmp/otel-addons/otel_span_fixture")
TOKEN = "otelTokensecret9c2e"
SLUG = "otelSlugsecret9c2e"
QUERY_SECRET = "serverQuerySecret9c2e"
CLIENT_QUERY_SECRET = "clientQuerySecret9c2e"
HEADER_SECRET = "fixtureHeaderSecret9c2e"
FIXTURE_ROUTE = "/api/otel-fixture/<token>/partner/<slug>"
SECRETS = (TOKEN, SLUG, QUERY_SECRET, CLIENT_QUERY_SECRET, HEADER_SECRET)


def _write_fixture_addon():
    """Tiny addon used only by this test. It is not part of the image."""
    package = FIXTURE_ADDON
    (package / "models").mkdir(parents=True, exist_ok=True)
    (package / "controllers").mkdir(parents=True, exist_ok=True)
    (package / "data").mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("from . import models\nfrom . import controllers\n")
    (package / "__manifest__.py").write_text(
        "{\n"
        '    "name": "otel span fixture",\n'
        '    "version": "1.0",\n'
        '    "depends": ["base"],\n'
        '    "data": ["data/cron.xml"],\n'
        '    "installable": True,\n'
        "}\n"
    )
    (package / "models" / "__init__.py").write_text("from . import fixture\n")
    (package / "models" / "fixture.py").write_text(
        "import os\n"
        "import requests\n"
        "from odoo import api, fields, models\n"
        "\n"
        "class OtelSpanFixture(models.Model):\n"
        "    _name = 'otel.span.fixture'\n"
        "    _description = 'otel span fixture'\n"
        "    name = fields.Char()\n"
        "\n"
        "    def ping_remote(self, url):\n"
        "        partners = self.env['res.partner'].sudo().search([], limit=1)\n"
        "        partners.read(['name'])\n"
        "        requests.get(\n"
        "            url,\n"
        "            timeout=5,\n"
        "            headers={'Authorization': 'Bearer %s'},\n"
        "        )\n"
        "        return True\n"
        "\n"
        "    @api.model\n"
        "    def cron_tick(self):\n"
        "        url = os.environ.get('OTEL_FIXTURE_URL')\n"
        "        if url:\n"
        "            self.sudo().ping_remote(url)\n"
        "        return True\n" % HEADER_SECRET
    )
    (package / "controllers" / "__init__.py").write_text("from . import main\n")
    (package / "controllers" / "main.py").write_text(
        "import os\n"
        "from odoo import http\n"
        "from odoo.http import request\n"
        "\n"
        "class OtelFixtureController(http.Controller):\n"
        "    @http.route(\n"
        "        '/api/otel-fixture/<string:token>/partner/<string:slug>',\n"
        "        type='http', auth='public', methods=['GET'], csrf=False,\n"
        "    )\n"
        "    def hit(self, token, slug, **kwargs):\n"
        "        url = os.environ.get('OTEL_FIXTURE_URL')\n"
        "        request.env['otel.span.fixture'].sudo().ping_remote(url)\n"
        "        return request.make_response('ok')\n"
    )
    (package / "data" / "cron.xml").write_text(
        "<odoo>\n"
        "    <record id=\"ir_cron_otel_fixture\" model=\"ir.cron\">\n"
        "        <field name=\"name\">Otel Fixture Job</field>\n"
        "        <field name=\"model_id\" ref=\"model_otel_span_fixture\"/>\n"
        "        <field name=\"state\">code</field>\n"
        "        <field name=\"code\">model.cron_tick()</field>\n"
        "        <field name=\"interval_number\">1</field>\n"
        "        <field name=\"interval_type\">minutes</field>\n"
        "        <field name=\"active\">True</field>\n"
        "        <field name=\"user_id\" ref=\"base.user_root\"/>\n"
        "    </record>\n"
        "</odoo>\n"
    )


def _write_conf():
    CONF.write_text(
        "\n".join(
            [
                "[options]",
                "admin_passwd = admin",
                "db_host = 127.0.0.1",
                "db_port = 5432",
                "db_user = odoo",
                "db_password = odoo",
                f"db_name = {DB_NAME}",
                "addons_path = "
                + ",".join(
                    [
                        str(ODOO_SRC / "odoo" / "addons"),
                        str(ODOO_SRC / "addons"),
                        str(ROOT / "src"),
                        str(FIXTURE_ADDON.parent),
                    ]
                ),
                "data_dir = /tmp/odoo-otel-data",
                "http_interface = 127.0.0.1",
                f"http_port = {HTTP_PORT}",
                "gevent_port = 18072",
                "workers = 3",
                "max_cron_threads = 1",
                "server_wide_modules = base,web,otel_tracing",
                "limit_memory_soft = 2147483648",
                "limit_memory_hard = 2684354560",
                "limit_time_cpu = 600",
                "limit_time_real = 1200",
                "limit_time_real_cron = 1200",
                "log_level = info",
                "list_db = False",
                "",
            ]
        )
    )


def main():
    if not (ODOO_SRC / "odoo-bin").is_file():
        raise SystemExit(f"Odoo checkout missing at {ODOO_SRC}")
    _write_fixture_addon()
    _write_conf()

    collector = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    collector_port = collector.server_address[1]
    threading.Thread(target=collector.serve_forever, daemon=True).start()
    print(f"collector http://127.0.0.1:{collector_port}/v1/traces", flush=True)

    init_log = Path("/tmp/otel-prefork-init.log")
    init_env = _base_env()
    init_cmd = [
        sys.executable,
        "odoo-bin",
        "-c",
        str(CONF),
        "-d",
        DB_NAME,
        "-i",
        "base,web,mail,crm_custom,otel_span_fixture",
        "--without-demo=all",
        "--stop-after-init",
        "--workers=0",
    ]
    print("initializing database (base, web, mail, crm_custom, otel_span_fixture)", flush=True)
    init_code = _run(init_cmd, init_env, init_log, timeout=900)
    if init_code != 0:
        tail = init_log.read_text(errors="replace")[-4000:]
        raise SystemExit(f"odoo init exited {init_code}\n{tail}")
    print("init ok", flush=True)
    _keep_only_fixture_cron()

    class _Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    upstream_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    fixture_url = (
        f"http://127.0.0.1:{upstream_port}/v1/ping"
        f"?access_token={CLIENT_QUERY_SECRET}&page=2"
    )
    print(f"upstream {fixture_url}", flush=True)

    server_env = _base_env()
    server_env.update(
        {
            "OTEL_ENABLED": "true",
            "OTEL_SERVICE_NAME": "bopp-odoo",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{collector_port}",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_SAMPLER": "always_on",
            "OTEL_BSP_SCHEDULE_DELAY": "100",
            "OTEL_BSP_EXPORT_TIMEOUT": "2000",
            "OTEL_INIT_TIMEOUT": "3",
            "OTEL_ODOO_METHOD_SPANS": "true",
            "OTEL_FIXTURE_URL": fixture_url,
        }
    )
    server_log = Path("/tmp/otel-prefork-server.log")
    proc, log = _start_odoo(server_env, server_log)
    print(f"odoo pid={proc.pid} workers=3 method_spans=on", flush=True)
    try:
        _wait_ready(server_log, proc)
        codes, elapsed = _burst(f"http://127.0.0.1:{HTTP_PORT}/web/health")
        print(f"requests={len(codes)} elapsed={elapsed:.2f}s codes={sorted(set(codes))}", flush=True)
        if any(code != 200 for code in codes):
            raise SystemExit(f"non-200 responses: {codes}")
        if elapsed > 20:
            raise SystemExit(f"requests took {elapsed:.1f}s; a worker may be hung")
        instance_ids = _wait_spans(expected=3)
        text = server_log.read_text(errors="replace")
        _assert_workers(text, instance_ids)
        _exercise_routes()
        _assert_span_tree()
        on_stats = _measure(f"http://127.0.0.1:{HTTP_PORT}{_fixture_path()}")
        print(_format_latency("on", on_stats), flush=True)
        _stop_odoo(proc, log)
        proc = None
        log = None

        off_env = dict(server_env)
        off_env["OTEL_ODOO_METHOD_SPANS"] = "false"
        off_log_path = Path("/tmp/otel-prefork-server-off.log")
        proc, log = _start_odoo(off_env, off_log_path)
        print(f"odoo pid={proc.pid} workers=3 method_spans=off", flush=True)
        _wait_ready(off_log_path, proc)
        off_stats = _measure(f"http://127.0.0.1:{HTTP_PORT}{_fixture_path()}")
        print(_format_latency("off", off_stats), flush=True)
        overhead = on_stats["p50"] - off_stats["p50"]
        print(
            f"LATENCY overhead_p50_ms={overhead:.2f} "
            f"on_p50_ms={on_stats['p50']:.2f} off_p50_ms={off_stats['p50']:.2f}",
            flush=True,
        )
        print(
            f"PASS workers=3 crm_custom spans={len(instance_ids)} "
            f"instance_ids={sorted(instance_ids)}",
            flush=True,
        )
    finally:
        if proc is not None:
            _stop_odoo(proc, log)
        collector.shutdown()
        upstream.shutdown()


def _wait_ready(log_path, proc):
    deadline = time.perf_counter() + 180
    url = f"http://127.0.0.1:{HTTP_PORT}/web/health"
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(errors="replace")[-4000:]
            raise SystemExit(f"odoo exited {proc.returncode} during startup\n{tail}")
        text = log_path.read_text(errors="replace")
        http_workers = text.count("Worker WorkerHTTP")
        if http_workers >= 3 and "HTTP service" in text:
            try:
                response = requests.get(url, timeout=5)
            except requests.RequestException:
                time.sleep(0.3)
                continue
            if response.status_code == 200:
                print(f"health 200 after seeing {http_workers} HTTP worker lines", flush=True)
                return
        time.sleep(0.3)
    tail = log_path.read_text(errors="replace")[-4000:]
    raise SystemExit(f"timed out waiting for /web/health\n{tail}")


def _burst(url):
    def hit(_n):
        response = requests.get(url, timeout=10)
        return response.status_code

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(hit, range(24)))
    return codes, time.perf_counter() - started


def _wait_spans(expected):
    deadline = time.perf_counter() + 20
    ids = set()
    while time.perf_counter() < deadline:
        ids = _server_instance_ids()
        if len(ids) >= expected:
            return ids
        # Another burst in case the first landed on fewer workers.
        _burst(f"http://127.0.0.1:{HTTP_PORT}/web/health")
        time.sleep(0.4)
    raise SystemExit(
        f"expected server spans from {expected} workers, got {len(ids)}: {sorted(ids)}"
    )


def _assert_workers(text, instance_ids):
    if "OpenTelemetry tracing deferred until after worker fork" not in text:
        raise SystemExit("parent did not defer tracing until after fork")
    if "threadpool_preloaded=True" not in text:
        raise SystemExit(
            "crm_custom preload did not import concurrent.futures.thread before workers forked"
        )
    enabled = text.count("OpenTelemetry tracing enabled pid=")
    if enabled < 3:
        raise SystemExit(f"expected tracing enabled on HTTP workers, saw {enabled}")
    if "Worker WorkerCron" not in text:
        raise SystemExit("cron worker never became alive")
    if "OpenTelemetry init timed out" in text or "OpenTelemetry init failed" in text:
        raise SystemExit("tracing init failed open unexpectedly during the healthy run")
    if len(instance_ids) < 3:
        raise SystemExit(f"spans came from {len(instance_ids)} workers")
    interesting = [
        line
        for line in text.splitlines()
        if "OpenTelemetry" in line or "Worker Worker" in line
    ]
    print("--- worker log lines ---", flush=True)
    print("\n".join(interesting), flush=True)


def _start_odoo(env, log_path):
    import subprocess

    log = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "odoo-bin",
            "-c",
            str(CONF),
            "-d",
            DB_NAME,
            "--workers=3",
            "--max-cron-threads=1",
        ],
        cwd=str(ODOO_SRC),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log


def _stop_odoo(proc, log):
    import subprocess

    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    if log is not None and not log.closed:
        log.close()


def _keep_only_fixture_cron():
    import psycopg2

    conn = psycopg2.connect(
        host="127.0.0.1",
        port=5432,
        user="odoo",
        password="odoo",
        dbname=DB_NAME,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE ir_cron SET active = false WHERE cron_name IS DISTINCT FROM %s",
            ("Otel Fixture Job",),
        )
        cur.execute(
            """
            UPDATE ir_cron
               SET active = true,
                   nextcall = (now() AT TIME ZONE 'UTC') - interval '1 minute'
             WHERE cron_name = %s
            """,
            ("Otel Fixture Job",),
        )
        if cur.rowcount != 1:
            cur.execute("SELECT id, cron_name, active FROM ir_cron ORDER BY id")
            raise SystemExit(f"fixture cron missing, ir_cron={cur.fetchall()}")
        conn.commit()
        print("fixture cron armed; other crons inactive", flush=True)
    finally:
        conn.close()


def _fixture_path():
    return (
        f"/api/otel-fixture/{TOKEN}/partner/{SLUG}"
        f"?access_token={QUERY_SECRET}&page=9"
    )


def _exercise_routes():
    base = f"http://127.0.0.1:{HTTP_PORT}"
    fixture = requests.get(base + _fixture_path(), timeout=20)
    print(f"fixture route {fixture.status_code} body={fixture.text[:120]!r}", flush=True)
    if fixture.status_code != 200 or fixture.text.strip() != "ok":
        raise SystemExit(f"fixture route failed: {fixture.status_code} {fixture.text[:400]}")
    partner = requests.get(f"{base}/api/partner/{SLUG}", timeout=20)
    print(f"partner route {partner.status_code}", flush=True)
    if partner.status_code >= 500:
        raise SystemExit(f"partner route failed: {partner.status_code} {partner.text[:400]}")
    zortout = requests.post(
        f"{base}/api/integrations/zortout/{TOKEN}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    print(f"zortout route {zortout.status_code}", flush=True)
    if zortout.status_code >= 500:
        raise SystemExit(f"zortout route failed: {zortout.status_code} {zortout.text[:400]}")


def _measure(url, count=30, warmup=5):
    for _ in range(warmup):
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            raise SystemExit(f"latency warmup failed: {response.status_code} {response.text[:200]}")
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        response = requests.get(url, timeout=20)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code != 200:
            raise SystemExit(f"latency sample failed: {response.status_code}")
        samples.append(elapsed_ms)
    return {"n": count, "p50": _percentile(samples, 50), "p95": _percentile(samples, 95)}


def _percentile(values, pct):
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _format_latency(label, stats):
    return (
        f"LATENCY method_spans={label} n={stats['n']} "
        f"p50_ms={stats['p50']:.2f} p95_ms={stats['p95']:.2f}"
    )


def _any_value(value):
    kind = value.WhichOneof("value")
    if kind is None:
        return ""
    raw = getattr(value, kind)
    if kind == "array_value":
        return [_any_value(item) for item in raw.values]
    if kind == "kvlist_value":
        return {item.key: _any_value(item.value) for item in raw.values}
    return raw


class _SpanView:
    def __init__(self, span):
        self.name = span.name
        self.kind = span.kind
        self.span_id = bytes(span.span_id)
        self.parent_id = bytes(span.parent_span_id)
        self.trace_id = bytes(span.trace_id)
        self.attributes = {item.key: _any_value(item.value) for item in span.attributes}
        self.events = []
        for event in span.events:
            self.events.append(
                {
                    "name": event.name,
                    "attributes": {item.key: _any_value(item.value) for item in event.attributes},
                }
            )

    def text(self):
        parts = [self.name]
        for key, value in self.attributes.items():
            parts.append(str(key))
            parts.append(str(value))
        for event in self.events:
            parts.append(event["name"])
            parts.append(str(event["attributes"]))
        return "\n".join(parts)


def _all_spans():
    with _SPAN_LOCK:
        batches = list(_SPANS)
    spans = []
    for batch in batches:
        for resource_spans in batch.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    spans.append(_SpanView(span))
    return spans


def _ancestor(span, by_id, predicate):
    current = span
    seen = set()
    while current.parent_id and current.parent_id not in seen:
        seen.add(current.parent_id)
        parent = by_id.get(current.parent_id)
        if parent is None:
            return None
        if predicate(parent):
            return parent
        current = parent
    return None


def _print_tree(spans):
    by_id = {span.span_id: span for span in spans}
    children = {}
    for span in spans:
        children.setdefault(span.parent_id, []).append(span)
    print("--- span tree ---", flush=True)

    def walk(span, indent):
        extras = []
        for key in (
            "http.route",
            "odoo.model",
            "odoo.method",
            "odoo.record_count",
            "odoo.cron.name",
            "odoo.cron.method",
            "code.function",
        ):
            if key in span.attributes:
                extras.append(f"{key}={span.attributes[key]}")
        statement = span.attributes.get("db.statement") or span.attributes.get("db.query.text")
        if statement:
            extras.append("sql=" + " ".join(str(statement).split())[:90])
        url = span.attributes.get("http.url") or span.attributes.get("url.full") or ""
        if url and span.kind == 3:
            extras.append(f"url={url}")
        suffix = (" " + " ".join(extras)) if extras else ""
        print(f"{'  ' * indent}- {span.name}{suffix}", flush=True)
        for child in children.get(span.span_id, []):
            walk(child, indent + 1)

    roots = [
        span
        for span in spans
        if not span.parent_id or span.parent_id not in by_id
    ]
    for root in roots:
        walk(root, 0)
    if not roots:
        print("(empty)", flush=True)


def _check_tree(spans):
    blob = "\n".join(span.text() for span in spans)
    for secret in SECRETS:
        if secret in blob:
            return f"secret {secret!r} leaked into a span:\n{blob[-2000:]}"
    if "page=2" not in blob:
        return "expected non-sensitive client query page=2 to remain on the outbound span"
    by_id = {span.span_id: span for span in spans}
    fixture = [
        span
        for span in spans
        if span.kind == 2 and span.name == f"GET {FIXTURE_ROUTE}"
    ]
    if not fixture:
        names = sorted({span.name for span in spans if span.kind == 2})
        return f"fixture server span missing, server spans={names}"
    server = fixture[-1]
    if server.attributes.get("http.route") != FIXTURE_ROUTE:
        return f"fixture http.route={server.attributes.get('http.route')!r}"
    for key in ("http.target", "url.path", "http.url", "url.full"):
        value = server.attributes.get(key)
        if isinstance(value, str) and value and FIXTURE_ROUTE not in value:
            return f"fixture {key}={value!r} does not use the route pattern"
    same_trace = [span for span in spans if span.trace_id == server.trace_id]
    controller = next(
        (
            span
            for span in same_trace
            if span.name == "controller OtelFixtureController.hit"
            and span.parent_id == server.span_id
        ),
        None,
    )
    if controller is None:
        return "controller span is not a direct child of the server span"
    if controller.attributes.get("code.function") != "hit":
        return f"controller code.function={controller.attributes.get('code.function')!r}"
    if controller.attributes.get("http.route") != FIXTURE_ROUTE:
        return f"controller http.route={controller.attributes.get('http.route')!r}"
    method = next(
        (span for span in same_trace if span.name == "otel.span.fixture.ping_remote"),
        None,
    )
    if method is None or _ancestor(method, by_id, lambda span: span.span_id == controller.span_id) is None:
        return "otel.span.fixture.ping_remote is not under the controller span"
    orm = next(
        (
            span
            for span in same_trace
            if span.name == "res.partner.search"
            and _ancestor(span, by_id, lambda item: item.span_id == method.span_id) is not None
        ),
        None,
    )
    if orm is None:
        return "res.partner.search is not under ping_remote"
    if orm.attributes.get("odoo.model") != "res.partner" or orm.attributes.get("odoo.method") != "search":
        return f"orm attributes={orm.attributes}"

    def _is_sql(span):
        statement = span.attributes.get("db.statement") or span.attributes.get("db.query.text") or ""
        return "SELECT" in str(statement).upper()

    sql = next(
        (
            span
            for span in same_trace
            if _is_sql(span)
            and _ancestor(span, by_id, lambda item: item.span_id == orm.span_id) is not None
        ),
        None,
    )
    if sql is None:
        return "SQL span is not under res.partner.search"
    client = next(
        (
            span
            for span in same_trace
            if span.kind == 3
            and _ancestor(span, by_id, lambda item: item.span_id == method.span_id) is not None
            and (
                "127.0.0.1" in span.text()
                or "/v1/ping" in span.text()
            )
        ),
        None,
    )
    if client is None:
        return "outbound HTTP client span is not under ping_remote"
    partner_route = next(
        (span for span in spans if span.kind == 2 and span.name == "GET /api/partner/<slug>"),
        None,
    )
    if partner_route is None:
        return "GET /api/partner/<slug> server span missing"
    zortout_route = next(
        (
            span
            for span in spans
            if span.kind == 2 and span.name == "POST /api/integrations/zortout/<token>"
        ),
        None,
    )
    if zortout_route is None:
        return "POST /api/integrations/zortout/<token> server span missing"
    zort_trace = [span for span in spans if span.trace_id == zortout_route.trace_id]
    if not any(span.name == "partner.get_partner_from_zortout_token" for span in zort_trace):
        return "zortout trace has no partner.get_partner_from_zortout_token span"
    if not any(
        span.name == "partner.search"
        and _ancestor(span, by_id, lambda item: item.name == "partner.get_partner_from_zortout_token")
        is not None
        for span in zort_trace
    ):
        return "zortout partner.search is not under get_partner_from_zortout_token"
    cron = next((span for span in spans if span.name == "cron Otel Fixture Job"), None)
    if cron is None:
        return "cron Otel Fixture Job span missing"
    if "cron_tick" not in str(cron.attributes.get("odoo.cron.method", "")):
        return f"cron method attribute={cron.attributes.get('odoo.cron.method')!r}"
    _print_tree(same_trace)
    return None


def _notify_cron():
    """Wake WorkerCron. It LISTENs on cron_trigger and otherwise sleeps 60s."""
    import psycopg2

    conn = psycopg2.connect(
        host="127.0.0.1",
        port=5432,
        user="odoo",
        password="odoo",
        dbname="postgres",
    )
    try:
        conn.autocommit = True
        conn.cursor().execute("SELECT pg_notify('cron_trigger', %s)", (DB_NAME,))
    finally:
        conn.close()


def _assert_span_tree():
    # The cron worker sleeps ~60s before its first poll unless NOTIFY wakes it.
    deadline = time.perf_counter() + 90
    last = "span tree not ready"
    while time.perf_counter() < deadline:
        try:
            _notify_cron()
        except Exception as exc:
            print(f"cron notify failed: {exc}", flush=True)
        error = _check_tree(_all_spans())
        if error is None:
            print("span tree ok", flush=True)
            return
        last = error
        time.sleep(0.5)
    _print_tree(_all_spans())
    raise SystemExit(last)


if __name__ == "__main__":
    main()
