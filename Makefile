SHELL := /usr/bin/env bash
SHELL_FILES := $(shell find controller scripts worker -type f -name '*.sh' 2>/dev/null)

.PHONY: agents-check agents-sync bootstrap-controller check cloud-init-check doctor format format-check install-agents lint omp-overlay omp-overlay-check test

agents-sync:
	python3 scripts/agents/agent_assets.py sync

agents-check:
	python3 scripts/agents/agent_assets.py check

bootstrap-controller:
	./controller/bootstrap.sh

install-agents:
	./controller/install-agents.sh

# Machine-local OMP wiring: lets an agent working in the wavCSE checkout reach
# the control plane's skills without copying them or changing directory.
omp-overlay:
	./controller/omp-overlay.sh

omp-overlay-check:
	./controller/omp-overlay.sh --check

doctor:
	uv run --locked infra doctor

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .
	@if [[ -n "$(SHELL_FILES)" ]]; then shfmt -w $(SHELL_FILES); fi

format-check:
	uv run --locked ruff format --check .
	@if [[ -n "$(SHELL_FILES)" ]]; then shfmt -d $(SHELL_FILES); fi

lint:
	uv run --locked ruff check .
	@if [[ -n "$(SHELL_FILES)" ]]; then shellcheck $(SHELL_FILES); fi

test:
	uv run --locked pytest

cloud-init-check:
	cloud-init schema --config-file controller/cloud-init.yaml

check:
	uv lock --check
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) cloud-init-check
	$(MAKE) agents-check
