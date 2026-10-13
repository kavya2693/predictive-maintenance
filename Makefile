.PHONY: data run check test lint format

data:
	uv run python scripts/generate_data.py

run: data
	uv run predmaint 2>&1 | tee outputs/run.log

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -q

check: lint test
