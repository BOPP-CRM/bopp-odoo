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
        "base,web,mail,crm_custom",
        "--without-demo=all",
        "--stop-after-init",
        "--workers=0",
    ]
    print("initializing database (base, web, mail, crm_custom)", flush=True)
    init_code = _run(init_cmd, init_env, init_log, timeout=900)
    if init_code != 0:
        tail = init_log.read_text(errors="replace")[-4000:]
        raise SystemExit(f"odoo init exited {init_code}\n{tail}")
    print("init ok", flush=True)

    server_log = Path("/tmp/otel-prefork-server.log")
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
        }
    )
    server_cmd = [
        sys.executable,
        "odoo-bin",
        "-c",
        str(CONF),
        "-d",
        DB_NAME,
        "--workers=3",
        "--max-cron-threads=1",
    ]
    import subprocess

    log = open(server_log, "w")
    proc = subprocess.Popen(
        server_cmd,
        cwd=str(ODOO_SRC),
        env=server_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"odoo pid={proc.pid} workers=3", flush=True)
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
        print(
            f"PASS workers=3 crm_custom spans={len(instance_ids)} "
            f"instance_ids={sorted(instance_ids)}",
            flush=True,
        )
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        log.close()
        collector.shutdown()


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


if __name__ == "__main__":
    main()
