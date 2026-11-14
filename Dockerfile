FROM odoo:18

USER root

# Debian ships typing_extensions without a pip RECORD, so pip cannot upgrade
# it. OpenTelemetry's protobuf build needs a newer release; replace that one
# file in place and leave the rest of the image's Python packages alone.
RUN apt-get update && \
    apt-get install -y gettext-base && \
    rm -rf /usr/lib/python3/dist-packages/typing_extensions.py \
           /usr/lib/python3/dist-packages/typing_extensions-4.10.0.dist-info && \
    python3 -m pip install --break-system-packages \
        'typing-extensions==4.16.0' \
        python-barcode \
        boto3 \
        'pydevd-pycharm~=253.32098.74' \
        'opentelemetry-api==1.45.2' \
        'opentelemetry-sdk==1.45.2' \
        'opentelemetry-exporter-otlp-proto-http==1.45.2' \
        'opentelemetry-exporter-otlp-proto-grpc==1.45.2' \
        'opentelemetry-instrumentation-wsgi==0.66b2' \
        'opentelemetry-instrumentation-psycopg2==0.66b2' \
        'opentelemetry-instrumentation-requests==0.66b2' \
        'opentelemetry-instrumentation-urllib3==0.66b2' \
        'opentelemetry-instrumentation-httpx==0.66b2' && \
    rm -rf /var/lib/apt/lists/*

COPY ./src /mnt/extra-addons
RUN chown -R odoo:odoo /mnt/extra-addons

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
