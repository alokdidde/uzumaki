.PHONY: check
# Offline check: no installs, network or live providers. Run after
# `python -m pip install -r requirements-dev.txt` and `python -m pip install --no-deps -e .`.
check:
	python -m ruff check .
	python -m ruff format --check .
	python -m mypy src
	python -m pytest
	python -m build --no-isolation --outdir build/dist
