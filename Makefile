.PHONY: check containment baseline-evidence sim-assets sim sim-demo live-smoke live-smoke-codex generate-live generate-live-codex demo-live demo-live-codex
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
	UZUMAKI_REQUIRE_CONTAINMENT=1 python -m pytest tests/test_sandbox_admission.py tests/test_broker.py tests/test_sandbox_containment.py \
		tests/test_generator_worker.py tests/test_promotion.py

# Issue-08 offline baseline suite, also retaining the raw event bundles of every frozen case and
# a hash manifest in build/evidence/baseline (ignored by Git; bundles are never committed).
baseline-evidence:
	rm -rf build/evidence/baseline
	UZUMAKI_BASELINE_EVIDENCE_DIR=build/evidence/baseline python -m pytest tests/test_baseline.py

# Optional MuJoCo SO-101 simulation (docs/features/so101-simulation.md). Needs
# `python -m pip install -r requirements-sim.txt` once; sim-assets downloads the pinned SO-101 model.
sim-assets:
	python -m uzumaki.sim.assets fetch

# Simulator suite with the missing-simulator skip turned into a failure.
sim:
	UZUMAKI_REQUIRE_SIM=1 python -m pytest tests/test_sim.py

# Every SO-101 case with bundles, summary and MP4 videos in build/sim (headless: MUJOCO_GL=osmesa).
sim-demo:
	rm -rf build/sim
	python -m uzumaki.sim --video --out build/sim

# Explicit live smoke run (never in CI): the Claude planner through the Vercel AI Gateway on one
# SO-101 case. Needs network access to ai-gateway.vercel.sh and AI_GATEWAY_API_KEY; keeps the
# exchanges so the run replays offline (`python -m uzumaki.sim --planner model --replay ...`).
live-smoke:
	rm -rf build/live
	python -m uzumaki.sim --planner model --case nominal --out build/live --record build/live-exchanges.json

# Same smoke run through the Codex CLI signed in with ChatGPT (`codex login --device-auth`).
live-smoke-codex:
	rm -rf build/live
	python -m uzumaki.sim --planner model --provider codex --case nominal --out build/live --record build/live-exchanges.json

# Explicit live generation run (never in CI): Claude writes the transfer-box skill through the
# gateway, repairs it on the 16 SO-101 development cases and the accepted program is confirmed on
# the 12 withheld cases. Needs an admitted restricted-worker host, gateway access and
# AI_GATEWAY_API_KEY; replays offline with `python -m uzumaki.sim.generate --replay ...`.
generate-live:
	rm -rf build/generate
	MUJOCO_GL=osmesa python -m uzumaki.sim.generate --video --out build/generate --record build/generate-exchanges.json

# Same generation run through the Codex CLI signed in with ChatGPT (`codex login --device-auth`).
generate-live-codex:
	rm -rf build/generate
	MUJOCO_GL=osmesa python -m uzumaki.sim.generate --provider codex --video --out build/generate --record build/generate-exchanges.json

# The whole loop from one command (never in CI): the planner abstains for lack of a skill, the
# generator writes and repairs transfer-box, it is confirmed on withheld cases and promoted, and
# the planner then completes the task with it. Bundles, library, summary and video in build/demo;
# replays offline with `python -m uzumaki.sim.demo --replay build/demo-exchanges.json ...`.
demo-live:
	rm -rf build/demo
	MUJOCO_GL=osmesa python -m uzumaki.sim.demo --video --out build/demo --record build/demo-exchanges.json

# Same demo through the Codex CLI signed in with ChatGPT (`codex login --device-auth`).
demo-live-codex:
	rm -rf build/demo
	MUJOCO_GL=osmesa python -m uzumaki.sim.demo --provider codex --video --out build/demo --record build/demo-exchanges.json
