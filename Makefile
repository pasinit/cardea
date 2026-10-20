SHELL := /bin/bash

.PHONY: help install try try-test try-check test api-test kernel-test standards-test m1-test m1-demo kernel-openapi spire-fit dbos-fit dbos-integration-fit temporal-fit absurd-fit hatchet-fit kernel-schema kernel-demo lint lint-api fix-lint format typecheck typecheck-api check-uv

API_DIR := apps/api
UV ?= $(shell command -v uv 2>/dev/null || printf "%s/bin/uv" "$$(python3 -m site --user-base)")

help:
	@printf "Cardea commands\n"
	@printf "  make install      Install project dependencies\n"
	@printf "  make try          Run the real daemon through crash/recovery, no SPIRE needed\n"
	@printf "  make test         Run all available tests\n"
	@printf "  make api-test     Run backend tests\n"
	@printf "  make kernel-test  Run portable kernel contract tests\n"
	@printf "  make standards-test Run identity, transport, and digest fit checks\n"
	@printf "  make m1-test      Run authenticated daemon acceptance with real providers\n"
	@printf "  make m1-demo      Run authenticated crash/restart demonstration\n"
	@printf "  make kernel-openapi Export the separate v0.2 kernel API\n"
	@printf "  make spire-fit    Run real SPIRE bootstrap and identity binding tests\n"
	@printf "  make dbos-fit     Run isolated DBOS SQLite/PostgreSQL experiments\n"
	@printf "  make dbos-integration-fit Run DBOS/Cardea commit and recovery tests\n"
	@printf "  make temporal-fit Run isolated Temporal recovery experiments\n"
	@printf "  make absurd-fit   Run isolated Absurd PostgreSQL experiments\n"
	@printf "  make hatchet-fit  Run isolated Hatchet embedded experiments\n"
	@printf "  make kernel-schema Print versioned kernel JSON schemas\n"
	@printf "  make kernel-demo  Run the two-process persistence demo\n"
	@printf "  make lint         Run linters\n"
	@printf "  make fix-lint     Apply safe lint and format fixes\n"
	@printf "  make typecheck    Run mypy for Python type checks\n"

check-uv:
	@test -x "$(UV)" || { \
		printf "uv is required. Install it with: python3 -m pip install --user uv\n" >&2; \
		exit 1; \
	}

install: check-uv
	cd $(API_DIR) && "$(UV)" sync

run: api-dev

api-dev: check-uv
	cd $(API_DIR) && PYTHONPATH=src "$(UV)" run uvicorn cardea.core.main:app --reload --host $(API_HOST) --port $(API_PORT)

web-dev:

test: api-test load-test m4-test try-test incident-demo-test

TRY := PYTHONPATH=apps/api/src:experiments/try "$(UV)" run --locked --project $(API_DIR)

try: check-uv
	$(TRY) python experiments/try/try_.py

try-test: check-uv
	$(TRY) pytest -q -p no:cacheprovider experiments/try

try-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/try
	"$(UV)" run --locked --project $(API_DIR) ruff format --check --config apps/api/pyproject.toml experiments/try
	MYPYPATH=apps/api/src:experiments/try "$(UV)" run --locked --project $(API_DIR) mypy --strict --explicit-package-bases experiments/try

DEMO := PYTHONPATH=apps/api/src:experiments/incident-demo "$(UV)" run --locked --project $(API_DIR)

.PHONY: incident-demo-test incident-demo-check
incident-demo-test: check-uv
	$(DEMO) pytest -q -p no:cacheprovider experiments/incident-demo

incident-demo-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/incident-demo
	"$(UV)" run --locked --project $(API_DIR) ruff format --check --config apps/api/pyproject.toml experiments/incident-demo
	MYPYPATH=apps/api/src:experiments/incident-demo "$(UV)" run --locked --project $(API_DIR) mypy --strict --explicit-package-bases experiments/incident-demo

api-test: check-uv
	cd $(API_DIR) && "$(UV)" run pytest

kernel-test: check-uv
	cd $(API_DIR) && "$(UV)" run pytest tests/kernel

standards-test: check-uv
	cd $(API_DIR) && "$(UV)" run pytest tests/standards tests/kernel/test_digest_compatibility.py

spire-fit: check-uv
	cd experiments/spire && "$(UV)" run --locked pytest

dbos-fit: check-uv
	cd experiments/dbos && "$(UV)" run --locked pytest

dbos-integration-fit: check-uv
	cd experiments/dbos-integration && "$(UV)" run --locked pytest

temporal-fit: check-uv
	cd experiments/temporal && "$(UV)" run --locked pytest

absurd-fit: check-uv
	cd experiments/absurd && "$(UV)" run --locked pytest

hatchet-fit: check-uv
	cd experiments/hatchet && "$(UV)" run --locked pytest

kernel-schema: check-uv
	@cd $(API_DIR) && PYTHONPATH=src "$(UV)" run python -m cardea.kernel.schema

kernel-demo: check-uv
	@cd $(API_DIR) && PYTHONPATH=src "$(UV)" run python -m cardea.kernel.smoke

lint: lint-api

fix-lint: check-uv
	cd $(API_DIR) && "$(UV)" run ruff check . --fix
	cd $(API_DIR) && "$(UV)" run ruff format .

format: fix-lint

lint-api: check-uv
	cd $(API_DIR) && "$(UV)" run ruff check .
	cd $(API_DIR) && "$(UV)" run ruff format --check .

typecheck-api: check-uv
	cd $(API_DIR) && "$(UV)" run mypy src tests

typecheck: typecheck-api

m1-test: check-uv
	"$(UV)" run --locked --project $(API_DIR) pytest -q -c experiments/m1/pytest.ini experiments/m1/tests

m1-demo: check-uv
	PYTHONPATH=apps/api/src:experiments/m1:experiments/spire "$(UV)" run --locked --project $(API_DIR) python experiments/m1/demo.py

kernel-openapi: check-uv
	PYTHONPATH=apps/api/src "$(UV)" run --locked --project $(API_DIR) python -m cardea.service.schema > packages/kernel-openapi.json

.PHONY: load-test load-check
load-test: check-uv
	"$(UV)" run --locked --project packages/cardea-load pytest packages/cardea-load/tests

load-check: load-test
	"$(UV)" run --locked --project packages/cardea-load ruff check packages/cardea-load
	"$(UV)" run --locked --project packages/cardea-load mypy packages/cardea-load/src packages/cardea-load/tests packages/cardea-load/tools

.PHONY: m2-host-fit m2-host-check
m2-host-fit: check-uv
	@test -n "$(M2_STATE)" || { printf "Set M2_STATE to a disposable VM state directory; see experiments/m2/README.md.\n" >&2; exit 1; }
	"$(UV)" run --locked --project $(API_DIR) python experiments/m2/vm.py fit --state "$(M2_STATE)"

m2-host-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/m2
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs experiments/m2

.PHONY: m3-test m3-check
m3-test: check-uv
	"$(UV)" run --locked --project $(API_DIR) pytest -q apps/api/tests/runtime

m3-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/m3
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs experiments/m3

.PHONY: m4-test m4-check
m4-test: check-uv
	"$(UV)" run --locked --project $(API_DIR) pytest -q apps/api/tests/runtime/test_delegation.py
	"$(UV)" run --locked --project $(API_DIR) pytest -q -o pythonpath=apps/api/src experiments/m4/test_sdk_fit.py

m4-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/m4
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs experiments/m4

.PHONY: m5-stage m5-check
m5-stage: check-uv
	@test -n "$(M5_STAGE)" || { printf "Set M5_STAGE to a new build-input directory.\n" >&2; exit 1; }
	python3 packaging/stage.py "$(M5_STAGE)" --uv "$(UV)"

m5-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml packaging experiments/m5
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs packaging experiments/m5
