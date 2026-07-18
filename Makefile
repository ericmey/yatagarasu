# Single source of truth for how this repo is built and checked.
#
# CI invokes these exact targets. Do not duplicate the command list into the
# workflow: two lists drift, and the drift is invisible until someone's "green
# locally" meets a red CI.

PKGS := core plugins/cmux plugins/agent-bridge plugins/discord

.PHONY: help install lint fmt fmt-check test check
help:
	@echo "install    dev deps + every subpackage, editable"
	@echo "lint       ruff check"
	@echo "fmt        ruff format (writes)"
	@echo "fmt-check  ruff format --check (read-only)"
	@echo "test       pytest"
	@echo "check      exactly what CI runs — run before opening a PR"

install:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"
	pip install $(foreach p,$(PKGS),-e $(p))

lint:
	ruff check .

fmt:
	ruff format .

fmt-check:
	ruff format --check .

test:
	pytest

# Order matters and is asserted: lint -> format -> tests, same as CI, because
# CI runs this target.
check: lint fmt-check test
