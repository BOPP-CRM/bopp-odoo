# Odoo dev helpers
# Usage: make <target>   e.g. `make upgrade` or `make upgrade MODULE=crm_custom`

DB      ?= crm_backend
MODULE  ?= crm_custom
SERVICE ?= odoo
DC      ?= docker compose
ODOO    ?= $(DC) exec -T $(SERVICE) odoo -c /etc/odoo/odoo.conf -d $(DB)

.PHONY: help up down restart logs shell psql upgrade update install test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start containers
	$(DC) up -d

down: ## Stop and remove containers
	$(DC) down

restart: ## Restart the odoo container
	$(DC) restart $(SERVICE)

logs: ## Tail odoo logs
	$(DC) logs -f $(SERVICE)

shell: ## Open a bash shell in the odoo container
	$(DC) exec $(SERVICE) bash

psql: ## Open psql on the odoo database
	$(DC) exec db psql -U odoo -d $(DB)

upgrade: ## Upgrade a module (MODULE=crm_custom). Reloads views/data without full restart.
	$(ODOO) -u $(MODULE) --stop-after-init --no-http

install: ## Install a module (MODULE=...)
	$(ODOO) -i $(MODULE) --stop-after-init --no-http

update: upgrade ## Alias for upgrade

odoo-shell: ## Open an Odoo interactive shell
	$(DC) exec $(SERVICE) odoo shell -c /etc/odoo/odoo.conf -d $(DB) --no-http
