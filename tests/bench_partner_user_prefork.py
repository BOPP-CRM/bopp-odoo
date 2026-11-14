"""Prefork timing for GET /api/partner/<slug>/user.

Boots Odoo 18 with workers=3 and crm_custom, stubs LINE, SMS, and Zortout
locally with fixed latency, and prints p50/p95 plus trace export checks.

    /tmp/odoo-venv/bin/python tests/bench_partner_user_prefork.py --expect sync
    /tmp/odoo-venv/bin/python tests/bench_partner_user_prefork.py --expect skip

``sync`` is the old path (every unchanged GET waits on Zortout). ``skip`` is
the fixed path (unchanged GET does not write and does not sync).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
ODOO_SRC = Path(os.environ.get("ODOO_SRC", "/tmp/odoo"))
BENCH = Path(os.environ.get("USER_BENCH_DIR", "/tmp/user-bench"))
CONF = BENCH / "odoo.conf"
DB_NAME = os.environ.get("USER_BENCH_DB", "user_profile_prefork")
HTTP_PORT = int(os.environ.get("USER_BENCH_HTTP_PORT", "18081"))
PYTHON = os.environ.get("USER_BENCH_PYTHON", sys.executable)

SLUG = "bench-user-perf"
LINE_USER = "Ubenchuser0001"
DISPLAY = "Bench User"
RENAMED = "Bench User Renamed"
PICTURE = "https://cdn.example/bench.png"
TOKEN = "line-token-bench"
RENAMED_TOKEN = "line-token-bench-renamed"

LINE_DELAY = 0.40
SMS_DELAY = 0.35
ZORTOUT_GET_DELAY = 0.45
ZORTOUT_POST_DELAY = 0.90

SAMPLES = 30
WARMUP = 18
BURST = 24

_COUNTS = {}
_COUNT_LOCK = threading.Lock()
_SPANS = []
_SPAN_LOCK = threading.Lock()


def _count(key):
    with _COUNT_LOCK:
        _COUNTS[key] = _COUNTS.get(key, 0) + 1


def _snapshot():
    with _COUNT_LOCK:
        return dict(_COUNTS)


def _reset_counts():
    with _COUNT_LOCK:
        _COUNTS.clear()


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)
        self._handle("POST")

    def log_message(self, fmt, *args):
        return

    def _handle(self, method):
        path = urlparse(self.path).path
        if path == "/stats":
            return self._json(200, _snapshot())
        if path == "/stats/reset":
            _reset_counts()
            return self._json(200, {})

        delay = {
            ("GET", "/v2/profile"): LINE_DELAY,
            ("GET", "/openapi/sms/balance"): SMS_DELAY,
            ("GET", "/v4/Contact/GetContacts"): ZORTOUT_GET_DELAY,
            ("GET", "/v4/Contact/GetContactDetail"): ZORTOUT_GET_DELAY,
            ("POST", "/v4/Contact/AddContact"): ZORTOUT_POST_DELAY,
            ("POST", "/v4/Contact/UpdateContact"): ZORTOUT_POST_DELAY,
        }.get((method, path), 0)
        if delay:
            time.sleep(delay)
        _count(f"{method} {path}")

        auth = self.headers.get("Authorization") or ""
        if path == "/v2/profile":
            name = RENAMED if "renamed" in auth else DISPLAY
            payload = {
                "userId": LINE_USER,
                "displayName": name,
                "pictureUrl": PICTURE,
            }
        elif path == "/openapi/sms/balance":
            payload = {"remaining_credit": 40}
        elif path == "/v4/Contact/GetContacts":
            payload = {"resCode": "200", "resDesc": "Success", "list": []}
        elif path in ("/v4/Contact/GetContactDetail", "/v4/Contact/AddContact", "/v4/Contact/UpdateContact"):
            payload = {
                "resCode": "200",
                "resDesc": "Success",
                "detail": {"id": 42, "code": "BOPP-bench"},
            }
        else:
            return self._json(404, {"error": "unknown", "path": path, "method": method})
        return self._json(200, payload)

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class _Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
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
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def _write_conf():
    BENCH.mkdir(parents=True, exist_ok=True)
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
                    ]
                ),
                f"data_dir = {BENCH / 'data'}",
                "http_interface = 127.0.0.1",
                f"http_port = {HTTP_PORT}",
                "gevent_port = 18083",
                "workers = 3",
                "max_cron_threads = 0",
                "server_wide_modules = base,web,otel_tracing",
                "limit_memory_soft = 2147483648",
                "limit_memory_hard = 2684354560",
                "limit_time_cpu = 600",
                "limit_time_real = 1200",
                "log_level = info",
                "list_db = False",
                "",
            ]
        )
    )


def _pythonpath(extra=""):
    parts = [str(ODOO_SRC), str(ROOT / "src")]
    if extra:
        parts.insert(0, extra)
    current = os.environ.get("PYTHONPATH", "")
    if current:
        parts.append(current)
    return os.pathsep.join(parts)


def _base_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = _pythonpath()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in list(env):
        if key.startswith("OTEL_"):
            del env[key]
    return env


def _pg():
    import psycopg2

    return psycopg2.connect(
        host="127.0.0.1",
        port=5432,
        user="odoo",
        password="odoo",
        dbname="postgres",
    )


def _db_exists():
    conn = _pg()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _module_installed():
    if not _db_exists():
        return False
    import psycopg2

    try:
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=5432,
            user="odoo",
            password="odoo",
            dbname=DB_NAME,
        )
    except psycopg2.OperationalError:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT state FROM ir_module_module WHERE name = %s",
            ("crm_custom",),
        )
        row = cur.fetchone()
        return bool(row and row[0] == "installed")
    except Exception:
        return False
    finally:
        conn.close()


def _run(cmd, env, log_path, timeout):
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
    if code != 0:
        tail = Path(log_path).read_text(errors="replace")[-4000:]
        raise SystemExit(f"command exited {code}: {cmd}\n{tail}")


def _init_db():
    if _module_installed():
        print("crm_custom already installed", flush=True)
        return
    log_path = BENCH / "init.log"
    cmd = [
        PYTHON,
        "odoo-bin",
        "-c",
        str(CONF),
        "-d",
        DB_NAME,
        "-i",
        "crm_custom",
        "--without-demo=all",
        "--stop-after-init",
        "--workers=0",
        "--max-cron-threads=0",
    ]
    print("initializing database", flush=True)
    _run(cmd, _base_env(), log_path, timeout=900)
    print("init ok", flush=True)


def _disable_crons():
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
        cur.execute("UPDATE ir_cron SET active = false WHERE active IS DISTINCT FROM false")
        conn.commit()
        print(f"crons disabled ({cur.rowcount})", flush=True)
    finally:
        conn.close()


def _seed():
    script = f"""
slug = {SLUG!r}
partner = env["partner"].search([("slug", "=", slug)], limit=1)
if not partner:
    partner = env["partner"].create({{"name": "Bench Partner", "slug": slug}})
user = env["crm.user"].search([
    ("line_user_id", "=", {LINE_USER!r}),
    ("partner_id", "=", partner.id),
], limit=1)
if not user:
    user = env["crm.user"].with_context(skip_zortout_auto_sync=True).create({{
        "display_name": {DISPLAY!r},
        "picture_url": {PICTURE!r},
        "line_user_id": {LINE_USER!r},
        "partner_id": partner.id,
    }})
else:
    user.with_context(skip_zortout_auto_sync=True).write({{
        "display_name": {DISPLAY!r},
        "picture_url": {PICTURE!r},
        "active": True,
    }})
partner.write({{
    "ui_crm_required_phone": True,
    "ui_crm_required_email": False,
    "zortout_enabled": True,
    "zortout_api_key": "bench-key",
    "zortout_api_secret": "bench-secret",
    "zortout_store_name": "bench-store",
}})
env.cr.commit()
print("SEEDED", partner.id, user.id, user.display_name)
"""
    log_path = BENCH / "seed.log"
    cmd = [
        PYTHON,
        "odoo-bin",
        "shell",
        "-c",
        str(CONF),
        "-d",
        DB_NAME,
        "--no-http",
        "--workers=0",
        "--max-cron-threads=0",
    ]
    print("seeding partner and user", flush=True)
    proc = subprocess.run(
        cmd,
        cwd=str(ODOO_SRC),
        env=_base_env(),
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    log_path.write_text(proc.stdout or "")
    if proc.returncode != 0 or "SEEDED" not in (proc.stdout or ""):
        raise SystemExit(f"seed failed ({proc.returncode})\n{(proc.stdout or '')[-4000:]}")
    print((proc.stdout or "").strip().splitlines()[-1], flush=True)


def _write_sitecustomize(stub_port):
    path = BENCH / "sitecustomize.py"
    path.write_text(
        "import os\n"
        "from urllib.parse import urlparse\n"
        "import requests\n"
        f"_STUB = 'http://127.0.0.1:{stub_port}'\n"
        "_HOSTS = {'api.line.me', 'open-api.zortout.com'}\n"
        "_orig = requests.sessions.Session.request\n"
        "def _request(self, method, url, **kwargs):\n"
        "    parsed = urlparse(url)\n"
        "    if parsed.hostname in _HOSTS:\n"
        "        url = _STUB + parsed.path\n"
        "        if parsed.query:\n"
        "            url = url + '?' + parsed.query\n"
        "    return _orig(self, method, url, **kwargs)\n"
        "requests.sessions.Session.request = _request\n"
    )


def _start_odoo(env, log_path):
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [
            PYTHON,
            "odoo-bin",
            "-c",
            str(CONF),
            "-d",
            DB_NAME,
            "--workers=3",
            "--max-cron-threads=0",
        ],
        cwd=str(ODOO_SRC),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log


def _stop_odoo(proc, log):
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    if log is not None and not log.closed:
        log.close()


def _wait_ready(log_path, proc):
    deadline = time.perf_counter() + 180
    url = f"http://127.0.0.1:{HTTP_PORT}/web/health"
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            tail = Path(log_path).read_text(errors="replace")[-4000:]
            raise SystemExit(f"odoo exited {proc.returncode} during startup\n{tail}")
        text = Path(log_path).read_text(errors="replace")
        if text.count("Worker WorkerHTTP") >= 3 and "HTTP service" in text:
            try:
                response = requests.get(url, timeout=5)
            except requests.RequestException:
                time.sleep(0.3)
                continue
            if response.status_code == 200:
                print("health 200", flush=True)
                return
        time.sleep(0.3)
    tail = Path(log_path).read_text(errors="replace")[-4000:]
    raise SystemExit(f"timed out waiting for /web/health\n{tail}")


def _user_url():
    return f"http://127.0.0.1:{HTTP_PORT}/api/partner/{SLUG}/user"


def _expected_body(name=DISPLAY):
    return {
        "display_name": name,
        "picture_url": PICTURE,
        "line_user_id": LINE_USER,
        "email": False,
        "phone": False,
        "force_verify_phone": True,
        "force_verify_email": False,
        "birth_date": False,
        "gender": False,
        "address": False,
        "custom_fields": {},
        "is_updated_user_info": False,
        "tier": {
            "code": "member",
            "name": "Member",
            "min_spending": 0.0,
            "max_spending": 999999999.0,
            "color": False,
            "image_url": False,
        },
    }


def _hit(token=TOKEN, timeout=30):
    started = time.perf_counter()
    response = requests.get(
        _user_url(),
        headers={
            "Authorization": f"Bearer {token}",
            "Connection": "close",
        },
        timeout=timeout,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return response, elapsed_ms


def _percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _trace_ids_from_spans():
    found = set()
    instances = set()
    names = set()
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
                    trace_id = bytes(span.trace_id).hex()
                    if span.kind == 2:
                        found.add(trace_id)
                        names.add(span.name)
                        if instance:
                            instances.add(instance)
    return found, instances, names


def _assert_workers(log_path):
    text = Path(log_path).read_text(errors="replace")
    if "OpenTelemetry tracing deferred until after worker fork" not in text:
        raise SystemExit("parent did not defer tracing until after fork")
    enabled = text.count("OpenTelemetry tracing enabled pid=")
    if enabled < 3:
        raise SystemExit(f"expected tracing enabled on 3 HTTP workers, saw {enabled}")
    if "threadpool_preloaded=True" not in text:
        raise SystemExit("crm_custom did not preload concurrent.futures.thread before fork")
    if "OpenTelemetry init timed out" in text or "OpenTelemetry init failed" in text:
        raise SystemExit("tracing init failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("sync", "skip"), required=True)
    args = parser.parse_args()
    if not (ODOO_SRC / "odoo-bin").is_file():
        raise SystemExit(f"Odoo checkout missing at {ODOO_SRC}")

    _write_conf()
    (BENCH / "data").mkdir(parents=True, exist_ok=True)
    _init_db()
    _disable_crons()

    stub = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    print(f"stub http://127.0.0.1:{stub_port}", flush=True)
    _write_sitecustomize(stub_port)

    _seed()

    collector = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    collector_port = collector.server_address[1]
    threading.Thread(target=collector.serve_forever, daemon=True).start()
    print(f"collector http://127.0.0.1:{collector_port}/v1/traces", flush=True)

    server_env = _base_env()
    server_env["PYTHONPATH"] = _pythonpath(str(BENCH))
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
            "SMS_API_ENDPOINT": f"http://127.0.0.1:{stub_port}",
            "SMS_API_TOKEN": "bench",
            "SMS_APP_KEY": "bench",
            "SMS_APP_SECRET": "bench",
            "USER_BENCH_STUB": f"http://127.0.0.1:{stub_port}",
        }
    )
    for key in (
        "THAIBULKSMS_OTP_KEY",
        "THAIBULKSMS_OTP_SECRET",
        "THAIBULKSMS_API_KEY",
        "THAIBULKSMS_API_SECRET",
    ):
        server_env.pop(key, None)

    log_path = BENCH / "server.log"
    proc, log = _start_odoo(server_env, log_path)
    print(f"odoo pid={proc.pid} workers=3", flush=True)
    try:
        _wait_ready(log_path, proc)
        _assert_workers(log_path)

        for _ in range(WARMUP):
            response, _elapsed = _hit()
            if response.status_code != 200:
                raise SystemExit(f"warmup failed {response.status_code} {response.text[:300]}")
        requests.post(f"http://127.0.0.1:{stub_port}/stats/reset", timeout=5)

        samples = []
        for _ in range(SAMPLES):
            response, elapsed = _hit()
            if response.status_code != 200:
                raise SystemExit(f"sample failed {response.status_code} {response.text[:300]}")
            body = response.json()
            if body != _expected_body():
                raise SystemExit(f"response JSON changed: {body}")
            trace = response.headers.get("X-Trace-Id") or ""
            if len(trace) != 32 or any(char not in "0123456789abcdef" for char in trace):
                raise SystemExit(f"missing X-Trace-Id: {trace!r}")
            samples.append(elapsed)
        measured = _snapshot()
        p50 = _percentile(samples, 50)
        p95 = _percentile(samples, 95)
        print(
            f"LATENCY expect={args.expect} n={SAMPLES} p50_ms={p50:.1f} p95_ms={p95:.1f} "
            f"min_ms={min(samples):.1f} max_ms={max(samples):.1f}",
            flush=True,
        )
        print(f"CALLS {json.dumps(measured, sort_keys=True)}", flush=True)

        zortout_gets = measured.get("GET /v4/Contact/GetContacts", 0) + measured.get(
            "GET /v4/Contact/GetContactDetail", 0
        )
        zortout_posts = measured.get("POST /v4/Contact/AddContact", 0) + measured.get(
            "POST /v4/Contact/UpdateContact", 0
        )
        sms_calls = measured.get("GET /openapi/sms/balance", 0)
        line_calls = measured.get("GET /v2/profile", 0)
        if line_calls != SAMPLES:
            raise SystemExit(f"expected {SAMPLES} LINE calls, got {line_calls}")
        if args.expect == "sync":
            if zortout_gets < SAMPLES or zortout_posts < SAMPLES:
                raise SystemExit(f"expected Zortout GET+POST on every sample, got {measured}")
            if sms_calls < SAMPLES:
                raise SystemExit(f"expected an SMS credit call per sample, got {sms_calls}")
            if p50 < 1500:
                raise SystemExit(f"sync p50 {p50:.1f}ms is too low; latency stub may not be on the path")
        else:
            if zortout_gets or zortout_posts:
                raise SystemExit(f"unchanged GET still called Zortout: {measured}")
            if sms_calls > 3:
                raise SystemExit(f"SMS credit cache missed too often: {sms_calls}")
            if p50 > 1200:
                raise SystemExit(f"skip p50 {p50:.1f}ms is still on the slow path")

        requests.post(f"http://127.0.0.1:{stub_port}/stats/reset", timeout=5)

        def burst_one(_n):
            response, elapsed = _hit(timeout=30)
            return response.status_code, elapsed, response.headers.get("X-Trace-Id") or "", response.text[:200]

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=12) as pool:
            burst = list(pool.map(burst_one, range(BURST)))
        burst_elapsed = time.perf_counter() - started
        codes = {item[0] for item in burst}
        trace_ids = [item[2] for item in burst]
        print(
            f"BURST n={BURST} elapsed={burst_elapsed:.2f}s codes={sorted(codes)}",
            flush=True,
        )
        if codes != {200}:
            raise SystemExit(f"burst failed: {burst[:3]}")
        if burst_elapsed > 90:
            raise SystemExit(f"burst took {burst_elapsed:.1f}s; a worker may be hung")
        if any(len(trace) != 32 for trace in trace_ids):
            raise SystemExit(f"burst missing X-Trace-Id: {trace_ids[:5]}")

        deadline = time.perf_counter() + 15
        exported, instances, names = set(), set(), set()
        while time.perf_counter() < deadline:
            exported, instances, names = _trace_ids_from_spans()
            if set(trace_ids).issubset(exported) and len(instances) >= 3:
                break
            time.sleep(0.4)
        missing = [trace for trace in trace_ids if trace not in exported]
        if missing:
            raise SystemExit(
                f"{len(missing)} X-Trace-Id values were not exported; sample {missing[:3]}"
            )
        if len(instances) < 3:
            raise SystemExit(f"spans came from {len(instances)} workers: {sorted(instances)}")
        user_spans = [name for name in names if "user" in name]
        print(
            f"TRACES workers={len(instances)} user_spans={user_spans[:4]} exported={len(exported)}",
            flush=True,
        )
        if not user_spans:
            raise SystemExit(f"no user server span in {sorted(names)[:12]}")

        before_rename = _snapshot()
        renamed, renamed_ms = _hit(token=RENAMED_TOKEN)
        if renamed.status_code != 200:
            raise SystemExit(f"renamed GET failed {renamed.status_code} {renamed.text[:300]}")
        renamed_body = renamed.json()
        if renamed_body["display_name"] != RENAMED:
            raise SystemExit(f"renamed body {renamed_body}")
        if set(renamed_body) != set(_expected_body()):
            raise SystemExit(f"renamed keys changed {sorted(renamed_body)}")
        deadline = time.perf_counter() + 5
        after_rename = _snapshot()
        while time.perf_counter() < deadline:
            after_rename = _snapshot()
            posts = after_rename.get("POST /v4/Contact/AddContact", 0) + after_rename.get(
                "POST /v4/Contact/UpdateContact", 0
            )
            before_posts = before_rename.get("POST /v4/Contact/AddContact", 0) + before_rename.get(
                "POST /v4/Contact/UpdateContact", 0
            )
            if posts > before_posts:
                break
            time.sleep(0.2)
        posts = after_rename.get("POST /v4/Contact/AddContact", 0) + after_rename.get(
            "POST /v4/Contact/UpdateContact", 0
        )
        before_posts = before_rename.get("POST /v4/Contact/AddContact", 0) + before_rename.get(
            "POST /v4/Contact/UpdateContact", 0
        )
        print(
            f"RENAME status=200 elapsed_ms={renamed_ms:.1f} zortout_post_delta={posts - before_posts}",
            flush=True,
        )
        if posts <= before_posts:
            raise SystemExit("changing display_name did not call Zortout")
        if renamed_ms < 800:
            raise SystemExit(f"renamed request {renamed_ms:.1f}ms did not wait on Zortout")

        print(f"PASS expect={args.expect} p50_ms={p50:.1f} p95_ms={p95:.1f}", flush=True)
    finally:
        _stop_odoo(proc, log)
        stub.shutdown()
        collector.shutdown()


if __name__ == "__main__":
    main()
