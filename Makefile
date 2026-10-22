.PHONY: check containment baseline-evidence sim-assets sim sim-demo sim-demo-panda sim-demo-aloha sim-demo-tidybot sim-demo-spot sim-demo-g1 sim-demo-go1 live-smoke live-smoke-codex generate-live generate-live-codex demo-live demo-live-codex
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
	python -m uzumaki.sim.assets fetch so101 panda aloha tidybot spot g1 go1 umi

# Simulator suite with the missing-simulator skip turned into a failure.
sim:
	UZUMAKI_REQUIRE_SIM=1 python -m pytest tests/test_sim.py tests/test_panda.py tests/test_aloha.py \
		tests/test_tidybot.py tests/test_spot.py tests/test_g1.py tests/test_go1.py

# Every SO-101 case with bundles, summary and MP4 videos in build/sim (headless: MUJOCO_GL=osmesa).
sim-demo:
	rm -rf build/sim
	python -m uzumaki.sim --video --out build/sim

# The same for the Franka Panda, in build/sim-panda.
sim-demo-panda:
	rm -rf build/sim-panda
	python -m uzumaki.sim --robot panda --video --out build/sim-panda

# ALOHA handing a bar between its two arms (handover-v1), in build/sim-aloha.
sim-demo-aloha:
	rm -rf build/sim-aloha
	python -m uzumaki.sim --robot aloha --video --out build/sim-aloha

# TidyBot driving between two tables to fetch a box (fetch-v1), in build/sim-tidybot.
sim-demo-tidybot:
	rm -rf build/sim-tidybot
	python -m uzumaki.sim --robot tidybot --video --out build/sim-tidybot

# Spot standing on its legs, picking and placing with its arm (pick-place-v1), in build/sim-spot.
sim-demo-spot:
	rm -rf build/sim-spot
	python -m uzumaki.sim --robot spot --video --out build/sim-spot

# The Unitree G1 humanoid on a stand, picking and placing with its left Dex3 hand, in build/sim-g1.
sim-demo-g1:
	rm -rf build/sim-g1
	python -m uzumaki.sim --robot g1 --video --out build/sim-g1

# The Unitree Go1 standing, picking and placing with a jaw on its neck, in build/sim-go1.
sim-demo-go1:
	rm -rf build/sim-go1
	python -m uzumaki.sim --robot go1 --video --out build/sim-go1

# Live model runs (never in CI). Pick the backend with PROVIDER and MODEL, for example
#   make demo-live PROVIDER=anthropic                    (ANTHROPIC_API_KEY)
#   make demo-live PROVIDER=openai MODEL=gpt-5           (OPENAI_API_KEY)
#   make demo-live PROVIDER=openrouter MODEL=qwen/qwen3-coder   (OPENROUTER_API_KEY)
#   make demo-live PROVIDER=ollama MODEL=qwen3           (local server, no key)
#   make demo-live PROVIDER=openai-compatible BASE_URL=https://host/v1 MODEL=m API_KEY_ENV=MY_KEY
#   make demo-live PROVIDER=codex                        (Codex CLI signed in with ChatGPT)
# Default: the Vercel AI Gateway (AI_GATEWAY_API_KEY). `python -m uzumaki.sim --help` lists the
# backends. Each run keeps its exchanges, so it replays offline with `--replay FILE`.
PROVIDER ?= vercel
MODEL ?=
BASE_URL ?=
API_KEY_ENV ?=
MODEL_OPTIONS = --provider $(PROVIDER) $(if $(MODEL),--model $(MODEL)) $(if $(BASE_URL),--base-url $(BASE_URL)) $(if $(API_KEY_ENV),--api-key-env $(API_KEY_ENV))

# The model planner on one SO-101 case.
live-smoke:
	rm -rf build/live
	python -m uzumaki.sim --planner model $(MODEL_OPTIONS) --case nominal --out build/live --record build/live-exchanges.json

# The model writes the transfer-box skill, repairs it on the 16 SO-101 development cases, and the
# accepted program is confirmed on the 12 withheld cases. Needs an admitted restricted-worker host.
generate-live:
	rm -rf build/generate
	MUJOCO_GL=osmesa python -m uzumaki.sim.generate $(MODEL_OPTIONS) --video --out build/generate --record build/generate-exchanges.json

# The whole loop from one command: the planner abstains for lack of a skill, the generator writes
# and repairs transfer-box, it is confirmed on withheld cases and promoted, and the planner then
# completes the task with it. Bundles, library, summary and video in build/demo.
demo-live:
	rm -rf build/demo
	MUJOCO_GL=osmesa python -m uzumaki.sim.demo $(MODEL_OPTIONS) --video --out build/demo --record build/demo-exchanges.json

# Shorthands kept for the Codex CLI.
live-smoke-codex generate-live-codex demo-live-codex:
	$(MAKE) $(@:-codex=) PROVIDER=codex
