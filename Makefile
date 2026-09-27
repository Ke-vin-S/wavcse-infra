SHELL := /usr/bin/env bash
SHELL_FILES := $(shell find controller scripts worker -type f -name '*.sh' 2>/dev/null)

.PHONY: check format format-check lint test

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

check:
	uv lock --check
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) test
