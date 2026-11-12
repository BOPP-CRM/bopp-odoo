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

`http/protobuf` talks to the collector's HTTP port (usually 4318). `grpc` uses the gRPC port (usually 4317). Prefer `http/protobuf`. The gevent longpolling process still supports `grpc`: that export runs on a real thread so it does not stall the gevent hub.

Export uses a `BatchSpanProcessor`. Failures are logged on the background thread and never raised into the request. Spans are dropped if the queue fills.

### What a trace contains

The server span is named `METHOD /path`. For `/api/partner/<slug>/...` the name uses a `{slug}` template and the slug is the attribute `bopp.partner.slug`. `http.route` is the path or that template. `http.response.status_code` is the HTTP status.

Child spans are the SQL statement text (placeholders, not bound values) and the outbound HTTP call. Query-string secrets (`access_token`, `token`, `signature`, `password`, `api_key`, and similar), URL userinfo, `Authorization` / `Cookie` headers, request and response bodies, and `db.statement.parameters` are removed before export. Header capture env vars are not turned on.

While a span is current, Odoo log lines gain `trace_id=<32 hex>` and `span_id=<16 hex>`. Outside a span those fields are `-`. Loki can match `trace_id` to the same id in Tempo.

### Process model

`otel_tracing` is a server-wide module (`server_wide_modules` includes it; the entrypoint adds it to a generated `odoo.conf` if a deployment's file omitted it). `post_load` runs in every process before workers are forked.

- `workers > 0`: the parent process only applies the library patches and registers an `after_fork` hook. Each HTTP worker and each cron worker creates its own provider and exporter after `os.fork`. The parent never calls `set_tracer_provider`, because that flag is copied into the child and cannot be replaced, and a batch thread started in the parent does not survive fork.
- `workers = 0`: one process, initialized immediately.
- Gevent longpolling is a separate `odoo gevent` process. It initializes tracing in that process after gevent's monkey patch.

Cron jobs have no HTTP server span. Their SQL and outbound HTTP calls are root spans.

### Rollout

1. Ship this image. With `OTEL_ENABLED` unset, behavior matches the current image aside from the server-wide module import and one startup log line.
2. The k8s manifests (BOPP-CRM/bopp-k8s) set the variables above on the Odoo deployment. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at the Tempo OTLP receiver or an OpenTelemetry collector.
3. Confirm in Grafana: open a trace for `GET /api/partner/{slug}/user` and check that the LINE profile call and the SMS credit calls (`api.line.me`, `apiv2.godysms.com`, `api-v2.thaibulksms.com`) appear as client spans next to the SQL spans. This change does not make those calls faster.

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
