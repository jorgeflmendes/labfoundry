.PHONY: test coverage lint format typecheck docs docs-serve quality build

test:
	python -m pytest

coverage:
	python -m pytest --cov=labfoundry --cov-branch --cov-report=term-missing --cov-fail-under=70

lint:
	python -m ruff check .
	python -m ruff format --check .

format:
	python -m ruff format .

typecheck:
	python -m mypy src

docs:
	python -m mkdocs build --strict

docs-serve:
	python -m mkdocs serve

quality: lint typecheck coverage docs

build:
	python -m build
