SHELL := /bin/bash

.PHONY: help install screenshots acceptance-check error-codes try try-test try-check teams teams-test test api-test check check-test kernel-test standards-test m1-test m1-demo kernel-openapi spire-fit dbos-fit dbos-integration-fit temporal-fit absurd-fit hatchet-fit cel-fit kernel-schema kernel-demo lint lint-api fix-lint format typecheck typecheck-api check-uv

API_DIR := apps/api
UV ?= $(shell command -v uv 2>/dev/null || printf "%s/bin/uv" "$$(python3 -m site --user-base)")

help:
	@printf "Cardea commands\n"
	@printf "  make install      Install project dependencies\n"
	@printf "  make try          Run the real daemon through crash/recovery, no SPIRE needed\n"
	@printf "  make teams        Walk through ADR-034's software team and trading desk\n"
	@printf "  make test         Run all available tests\n"
	@printf "  make api-test     Run backend tests\n"
	@printf "  make check        Run lint, typecheck and the tests that need no TCP loopback\n"
	@printf "  make kernel-test  Run portable kernel contract tests\n"
	@printf "  make standards-test Run identity, transport, and digest fit checks\n"
	@printf "  make m1-test      Run authenticated daemon acceptance with real providers\n"
	@printf "  make m1-demo      Run authenticated crash/restart demonstration\n"
	@printf "  make kernel-openapi Export the separate v0.2 kernel API\n"
	@printf "  make error-codes  Regenerate docs/error-codes.md from the source\n"
	@printf "  make acceptance-check Lint the Debian acceptance scripts\n"
	@printf "  make try-toolchain Build and pin the env.try toolchain image (root, Debian host)\n"
	@printf "  make spire-fit    Run real SPIRE bootstrap and identity binding tests\n"
	@printf "  make dbos-fit     Run isolated DBOS SQLite/PostgreSQL experiments\n"
	@printf "  make dbos-integration-fit Run DBOS/Cardea commit and recovery tests\n"
	@printf "  make temporal-fit Run isolated Temporal recovery experiments\n"
	@printf "  make absurd-fit   Run isolated Absurd PostgreSQL experiments\n"
	@printf "  make hatchet-fit  Run isolated Hatchet embedded experiments\n"
	@printf "  make cel-fit      Run the ADR-036 CEL implementation fit test\n"
	@printf "  make kernel-schema Print versioned kernel JSON schemas\n"
	@printf "  make kernel-demo  Run the two-process persistence demo\n"
	@printf "  make lint         Run linters\n"
	@printf "  make fix-lint     Apply safe lint and format fixes\n"
	@printf "  make typecheck    Run mypy for Python type checks\n"

check-uv:
	@test -x "$(UV)" || { \
		printf "uv is required but was not found (looked for: %s).\n" "$(UV)" >&2; \
		printf "  Install it:   python3 -m pip install --user uv\n" >&2; \
		printf "  Then either put ~/.local/bin on PATH, or rerun make as it is (it also looks in the Python user base).\n" >&2; \
		printf "  Or point at an existing uv:   make UV=/path/to/uv try\n" >&2; \
		exit 1; \
	}

install: check-uv
	cd $(API_DIR) && "$(UV)" sync

run: api-dev

api-dev: check-uv
	cd $(API_DIR) && PYTHONPATH=src "$(UV)" run uvicorn cardea.core.main:app --reload --host $(API_HOST) --port $(API_PORT)

web-dev:

test: api-test load-test m4-test try-test incident-demo-test teams-test

TRY := PYTHONPATH=apps/api/src:experiments/try "$(UV)" run --locked --project $(API_DIR)

try: check-uv
	$(TRY) python experiments/try/try_.py

try-test: check-uv
	$(TRY) pytest -q -p no:cacheprovider experiments/try

TEAMS := PYTHONPATH=apps/api/src:examples/teams "$(UV)" run --locked --project $(API_DIR)

teams: check-uv
	$(TEAMS) python examples/teams/walkthrough.py
	$(TEAMS) python examples/teams/dev_team.py

teams-test: check-uv
	$(TEAMS) pytest -q -p no:cacheprovider examples/teams

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

# Parallel (pytest-xdist), one worker per core; PYTEST_WORKERS=0 runs serially.
PYTEST_WORKERS ?= auto

api-test: check-uv
	cd $(API_DIR) && "$(UV)" run pytest -n $(PYTEST_WORKERS)

# The `check` scenario of env.try: lint, typecheck and the tests that need no TCP loopback.
check:
	python3 packaging/check.py

# The `screenshots` scenario of env.try: the mock pages the commit adds under docs/mocks (or
# PAGES, paths in the repository), else a fixture page, at 390 px and desktop width.
screenshots:
	python3 packaging/screenshots.py $(PAGES)

check-test: check-uv
	cd $(API_DIR) && CARDEA_NO_LOOPBACK=1 "$(UV)" run pytest -m "not loopback" -q -rfE --tb=short -p no:cacheprovider

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

cel-fit: check-uv
	cd experiments/cel && "$(UV)" run --locked pytest
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/cel
	"$(UV)" run --locked --project $(API_DIR) ruff format --check --config apps/api/pyproject.toml experiments/cel

kernel-schema: check-uv
	@cd $(API_DIR) && PYTHONPATH=src "$(UV)" run python -m cardea.kernel.schema

kernel-demo: check-uv
	@cd $(API_DIR) && PYTHONPATH=src "$(UV)" run python -m cardea.kernel.smoke

lint: lint-api

REFERENCE_OPS := ../../packages/cardea-email-ops/src ../../packages/cardea-reference-ops/src ../../packages/cardea-vcs-ops/src ../../packages/cardea-coding-ops/src ../../packages/cardea-env-ops/src ../../examples ../../deploy/team-kit

fix-lint: check-uv
	cd $(API_DIR) && "$(UV)" run ruff check . $(REFERENCE_OPS) --fix
	cd $(API_DIR) && "$(UV)" run ruff format . $(REFERENCE_OPS)

format: fix-lint

lint-api: check-uv
	cd $(API_DIR) && "$(UV)" run ruff check . $(REFERENCE_OPS)
	cd $(API_DIR) && "$(UV)" run ruff format --check . $(REFERENCE_OPS)

typecheck-api: check-uv
	cd $(API_DIR) && "$(UV)" run mypy src tests ../../packages/cardea-email-ops/src ../../packages/cardea-reference-ops/src ../../packages/cardea-vcs-ops/src ../../packages/cardea-coding-ops/src ../../packages/cardea-env-ops/src ../../examples/teams ../../deploy/team-kit

typecheck: typecheck-api

m1-test: check-uv
	"$(UV)" run --locked --project $(API_DIR) pytest -q -c experiments/m1/pytest.ini experiments/m1/tests

m1-demo: check-uv
	PYTHONPATH=apps/api/src:experiments/m1:experiments/spire "$(UV)" run --locked --project $(API_DIR) python experiments/m1/demo.py

error-codes: check-uv
	PYTHONPATH=apps/api/src "$(UV)" run --locked --project $(API_DIR) python -m cardea.service.error_codes > docs/error-codes.md

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

.PHONY: try-toolchain
# ADR-038 decision 6: the host owner builds the env.try toolchain image with mmdebstrap and
# pins its digest in the repository registry; the kit's timer refreshes it weekly.
TRY_REGISTRY ?= /opt/cardea-code/repositories.json
try-toolchain:
	digest="$$(sudo python3 -I packaging/try_toolchain.py build --registry $(TRY_REGISTRY))" && \
		sudo python3 -I packaging/try_toolchain.py pin $(TRY_REGISTRY) --sha256 "$$digest"

.PHONY: acceptance-check
# The Debian acceptance scripts (docs/acceptance/debian-adr-034-037.md): lint and types only;
# they run on a Debian host, never here.
acceptance-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml experiments/debian-acceptance
	"$(UV)" run --locked --project $(API_DIR) ruff format --check --config apps/api/pyproject.toml experiments/debian-acceptance
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs experiments/debian-acceptance

.PHONY: m5-stage m5-check
m5-stage: check-uv
	@test -n "$(M5_STAGE)" || { printf "Set M5_STAGE to a new build-input directory.\n" >&2; exit 1; }
	python3 packaging/stage.py "$(M5_STAGE)" --uv "$(UV)"

m5-check: check-uv
	"$(UV)" run --locked --project $(API_DIR) ruff check --config apps/api/pyproject.toml packaging experiments/m5
	"$(UV)" run --locked --project $(API_DIR) mypy --follow-imports=silent --ignore-missing-imports --check-untyped-defs packaging experiments/m5
