.PHONY: help install lint fmt test check
help:
	@echo "install  install dev dependencies"
	@echo "lint     ruff check"
	@echo "fmt      ruff format"
	@echo "test     pytest"
	@echo "check    what CI runs — run this before opening a PR"

install:
	python -m pip install --upgrade pip -e ".[dev]"

lint:
	ruff check .

fmt:
	ruff format .

test:
	pytest

check: lint test
	ruff format --check .
