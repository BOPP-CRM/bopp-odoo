FROM odoo:18

USER root

RUN apt-get update && \
    apt-get install -y gettext-base && \
    python3 -m pip install --break-system-packages python-barcode boto3 pydevd-pycharm~=253.32098.74 && \
    rm -rf /var/lib/apt/lists/*

COPY ./src /mnt/extra-addons
RUN chown -R odoo:odoo /mnt/extra-addons

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
