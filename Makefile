.PHONY: install test lint typecheck format check clean demo backtest

install:
	pip install -e ".[dev]"

test:
	python -m pytest tests/ -v --cov=src/dfm --cov-report=term-missing

test-fast:
	python -m pytest tests/ -q

lint:
	python -m ruff check src/ tests/

typecheck:
	python -m mypy src/dfm

format:
	python -m ruff check --fix src/ tests/

check: lint typecheck test

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

demo:
	python -m dfm demo

backtest:
	python -m dfm backtest --samples 48 --amplitude 0.0005
