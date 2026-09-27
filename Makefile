SHELL := /usr/bin/env bash
SHELL_FILES := $(shell find controller scripts worker -type f -name '*.sh' 2>/dev/null)

.PHONY: bootstrap-controller check cloud-init-check doctor format format-check lint test

bootstrap-controller:
	./controller/bootstrap.sh

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
