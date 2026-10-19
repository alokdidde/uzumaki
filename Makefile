.PHONY: check containment
# Offline check: no installs, network or live providers. Run after
# `python -m pip install -r requirements-dev.txt` and `python -m pip install --no-deps -e .`.
check:
	python -m ruff check .
	python -m ruff format --check .
	python -m mypy src
	python -m pytest
	python -m build --no-isolation --outdir build/dist

# Restricted-worker suites only, with the platform skip turned into a failure: on a host that
# fails admission (see docs/features/restricted-worker.md) this exits non-zero instead of skipping.
containment:
	UZUMAKI_REQUIRE_CONTAINMENT=1 python -m pytest tests/test_sandbox_admission.py tests/test_broker.py tests/test_sandbox_containment.py
