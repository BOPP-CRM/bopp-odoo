#!/bin/bash
set -e

install_python_dependencies() {
    if [ "$(id -u)" = "0" ]; then
        python3 -m pip install --break-system-packages -q boto3 python-barcode
    fi
}

run_as_odoo() {
    if [ "$(id -u)" = "0" ]; then
        exec runuser -u odoo -- "$@"
    fi

    exec "$@"
}

install_python_dependencies
envsubst < /etc/odoo/odoo.conf.template > /etc/odoo/odoo.conf

if [ "$(id -u)" = "0" ]; then
    chown odoo:odoo /etc/odoo/odoo.conf
fi

if [ "$SERVICE" = "migrator" ]; then
    echo "Running migration..."
    run_as_odoo /usr/bin/odoo -c /etc/odoo/odoo.conf -d crm_backend -u crm_custom --without-demo=all --stop-after-init
else
    if [ "$DEBUG" = "1" ]; then
        echo "Starting Odoo with debug..."
        run_as_odoo python3 /mnt/extra-addons/debug_connect.py \
            -c /etc/odoo/odoo.conf \
            -d crm_backend \
            --without-demo=all \
            -u crm_custom
    else
        echo "Starting Odoo..."
        run_as_odoo /usr/bin/odoo -c /etc/odoo/odoo.conf -d crm_backend --without-demo=all -u crm_custom
    fi
fi
