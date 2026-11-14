import logging

_logger = logging.getLogger(__name__)


def post_load():
    """Server-wide hook. Runs in every Odoo process before workers are forked.

    A failure here must not stop Odoo. Tracing is optional and stays off
    unless OTEL_ENABLED=true and an OTLP endpoint is configured.
    """
    try:
        from .bootstrap import setup

        setup()
    except Exception:
        _logger.exception("OpenTelemetry setup failed; continuing without tracing")
