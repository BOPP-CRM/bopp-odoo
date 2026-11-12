# bopp-odoo

Custom Odoo 18 image (`ghcr.io/bopp-crm/bopp-odoo`) for the BOPP CRM backend.

## OpenTelemetry tracing

Each incoming HTTP request can be a trace. Postgres queries (`psycopg2`) and outbound HTTP calls (`requests` and `urllib3`, which covers boto3) are child spans, so a waterfall shows where the time went. Spans are exported with OTLP to Grafana Tempo.

Tracing is off unless the process is explicitly switched on. Odoo then starts and serves traffic as it does today: no library is patched and no exporter thread is started.

### Environment

The deployment sets these. The image does not hard-code an endpoint.

| Variable | Role |
| --- | --- |
| `OTEL_ENABLED` | Switch. Only the value `true` (any case) turns tracing on. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP base URL. Tracing stays off when this and `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` are both unset. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Optional traces-only override of the endpoint. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` (default) or `grpc`. `http/json` is sent as `http/protobuf`. |
| `OTEL_SERVICE_NAME` | Resource `service.name`. Defaults to `bopp-odoo` when unset. |
| `OTEL_TRACES_SAMPLER` | Standard SDK sampler name (`parentbased_always_on`, `always_on`, `always_off`, `traceidratio`, `parentbased_traceidratio`, …). |
| `OTEL_TRACES_SAMPLER_ARG` | Sampler argument, such as the ratio for `traceidratio`. |
| `OTEL_BSP_EXPORT_TIMEOUT` | Batch export timeout in milliseconds. Default `5000`, so a dead collector cannot hold worker shutdown for the SDK's 30s. |
| `OTEL_INIT_TIMEOUT` | Seconds to wait for tracer init inside `Worker.start`. Default `3`. On timeout the worker serves without tracing. |
| `OTEL_ODOO_METHOD_SPANS` | `true` (default) records ORM, custom-method, and cron spans. `false` leaves controller, SQL, and outbound HTTP spans in place. |
| `OTEL_ODOO_METHOD_ALLOWLIST` | Comma-separated model names (`res.partner`) or `model.method` pairs. Empty means every model. Does not filter controller or cron spans. |
| `OTEL_ODOO_METHOD_MIN_DURATION_MS` | Omit exported ORM and custom method spans shorter than this many milliseconds. Default `0` (keep the tree intact). |

`http/protobuf` talks to the collector's HTTP port (usually 4318). `grpc` uses the gRPC port (usually 4317). Prefer `http/protobuf`. The gevent longpolling process still supports `grpc`: that export runs on a real thread so it does not stall the gevent hub.

Export uses a `BatchSpanProcessor`. Failures are logged on the background thread and never raised into the request. Spans are dropped if the queue fills.

### What a trace contains

A request waterfall is nested:

```
POST /api/integrations/zortout/<token>          server
  controller ZortoutWebhookController.receive_webhook
    partner.get_partner_from_zortout_token      custom method
      partner.search                            ORM
        SELECT ...                              SQL
    GET                                         outbound HTTP
```

The server span is named `METHOD` plus the matched route pattern, not the raw path. `http.route`, `http.target`, `url.path`, and `http.url` use that same pattern (`/api/integrations/zortout/<token>`, `/api/partner/<slug>/user`). Query strings are removed from server spans. Path parameters such as the partner slug are not copied onto attributes.

The controller span carries `code.namespace`, `code.function`, and `http.route`. ORM spans are named `model.method` (`res.partner.write`) and record `odoo.model`, `odoo.method`, and `odoo.record_count` for `create`, `write`, `unlink`, `search`, `search_read`, and `read`. Public methods defined in BOPP addons (`crm_custom` and other extra addons) get the same kind of span, so the SQL they issue sits underneath the function that ran it. `ir.cron` jobs get one span each, `cron <job name>`, with `odoo.cron.method` when the server action calls `model.some_method()`.

Skipped on purpose, to keep traces small: private and dunder methods (this includes compute, inverse, and onchange helpers), methods decorated as a compute, onchange, or constraint, and recordset plumbing (`sudo`, `with_context`, `mapped`, `filtered`, and the same family). Core models are not wrapped method-by-method; their `create` / `write` / `unlink` / `search` / `search_read` / `read` calls are. Cron helpers stay private; the job span is the `_callback` around the server action.

Child spans also include the SQL statement text (placeholders, not bound values) and outbound HTTP from `requests` and `urllib3` (boto3). `httpx` is instrumented when that library is installed. Query-string secrets (`access_token`, `token`, `signature`, `password`, `api_key`, `key`, and similar), URL userinfo, `Authorization` / `Cookie` headers, request and response bodies, and `db.statement.parameters` are removed before export. Header capture is not turned on. Non-sensitive client query parameters such as `page` are kept.

While a span is current, Odoo log lines gain `trace_id=<32 hex>` and `span_id=<16 hex>`. Outside a span those fields are `-`. Loki can match `trace_id` to the same id in Tempo.

### Process model

`otel_tracing` is a server-wide module (`server_wide_modules` includes it; the entrypoint adds it to a generated `odoo.conf` if a deployment's file omitted it). `post_load` runs in every process before workers are forked.

- `workers > 0`: the parent process only applies the library patches. Each HTTP worker and each cron worker creates its own provider when `Worker.start` runs, which is after `os.fork` returns. The parent never calls `set_tracer_provider`, because that flag is copied into the child and cannot be replaced, and a batch thread started in the parent does not survive fork. Init is not done from `os.register_at_fork`: that callback runs while `concurrent.futures.thread`'s fork lock is still held (boto3 loads that module during registry preload) and `ThreadPoolExecutor.submit` then deadlocks the worker. If init raises or exceeds `OTEL_INIT_TIMEOUT` (default 3s), the worker logs a warning and serves traffic without tracing.
- `workers = 0`: one process, initialized immediately.
- Gevent longpolling is a separate `odoo gevent` process. It initializes tracing in that process after gevent's monkey patch.

Cron jobs have no HTTP server span. Each job has its own `cron <name>` root span, and the SQL and outbound HTTP it issues are children of that span.

### Rollout

1. Ship this image. With `OTEL_ENABLED` unset, behavior matches the current image aside from the server-wide module import and one startup log line.
2. The k8s manifests (BOPP-CRM/bopp-k8s) set the variables above on the Odoo deployment. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at the Tempo OTLP receiver or an OpenTelemetry collector.
3. Confirm in Grafana: open a trace for `GET /api/partner/<slug>/user` or `POST /api/integrations/zortout/<token>`. The route pattern is the span name (the token and slug are not attributes). Under the controller span, ORM and custom-method spans parent the SQL. LINE and SMS calls (`api.line.me`, `apiv2.godysms.com`, `api-v2.thaibulksms.com`) appear as client spans under the method that made them.

### Verify locally

Build the image, then start Odoo twice.

Disabled (default). Requests succeed and logs have no `trace_id=`:

```bash
docker compose up --build -d
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8069/web/health
```

Enabled, exporting at a closed port. The health check must still succeed, worker logs must contain `OpenTelemetry tracing enabled`, and a request log line during that request must contain `trace_id=` plus 32 hex digits. Exporter errors stay on the background thread.

```bash
# workers=0 is the compose default. For the prefork path, set workers=2
# in config/odoo.conf.template and recreate the container.
OTEL_ENABLED=true \
OTEL_SERVICE_NAME=bopp-odoo \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
OTEL_TRACES_SAMPLER=always_on \
docker compose up --build -d
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8069/web/health
docker compose logs odoo | grep 'OpenTelemetry tracing'
```

With `workers` greater than 0, the parent log says tracing is deferred until after fork, and each worker log says tracing is enabled with that worker's pid.

Unit checks for redaction, the post-fork provider, and a dead exporter live in `tests/test_otel_tracing.py`.
