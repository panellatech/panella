# Panella reproducible eval harness (defensive-parity bundle). Owns ONLY namespaced `eval-*` phony
# targets — deliberately NOT wired into CI in this slice (a comment, not an accident: the eval box
# needs a real docker daemon + optionally an LLM key/codex subprocess, which is a heavier and
# slower dependency than the CI `test`/`boot_check` jobs already carry; CI integration is a later
# slice once the harness has proven itself in local use). Every target is runnable from a fresh
# clone with only docker + python + (for QA/key_correctness) an API key or a local `codex` CLI.
#
# ISOLATION (mandatory mechanics, not vibes): every target that touches compose — up, exec, mint,
# ps, down — goes through $(EVAL_COMPOSE), with NO exceptions. The explicit --env-file is REQUIRED:
# without it, `docker compose` auto-loads the repo root's real `.env` (the operator's actual
# PANELLA_API_KEY), which would mean the "isolated" eval box shares a secret with a real box.
EVAL_COMPOSE = docker compose -p panella-eval --env-file eval/out/compose.env -f docker-compose.yml -f eval/compose.eval.yml

PYTHON ?= python3
DATASET_URL = https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
DATASET_FILE = eval/data/longmemeval_s_cleaned.json
# sha256 of the file at DATASET_URL, pinned from a REAL verified download (2026-07-07: 277,383,467
# bytes, 500 questions, independently re-hashed with `shasum -a 256` outside this Makefile to
# confirm) — `make eval-dataset` verifies every future download against this before anything reads
# it. If HuggingFace ever republishes the file with a legitimate content change, update this
# constant deliberately (never silently, and never with an invented hash).
DATASET_NAME = longmemeval_s_cleaned.json
DATASET_SHA256 = d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442

.PHONY: eval-dataset eval-up eval-down eval-retrieve eval-qa eval-report eval-smoke eval-selftest eval-isolation-check eval-public-scan eval-visibility-canary

# --------------------------------------------------------------------------------------------
# Dataset

eval-dataset:
	@mkdir -p eval/data
	@if [ -f "$(DATASET_FILE)" ]; then \
		echo "eval-dataset: $(DATASET_FILE) already present, verifying sha256"; \
	else \
		echo "eval-dataset: downloading LongMemEval dataset (~277MB) — this is NOT committed (eval/data/ is gitignored)"; \
		curl -fL --retry 3 -o "$(DATASET_FILE)" "$(DATASET_URL)"; \
	fi
	@actual_sha=$$(shasum -a 256 "$(DATASET_FILE)" | awk '{print $$1}'); \
	if [ "$(DATASET_SHA256)" = "UNVERIFIED_PIN_ON_FIRST_REAL_DOWNLOAD" ]; then \
		echo "eval-dataset: DATASET_SHA256 is unpinned — this Makefile has never verified a real download."; \
		echo "eval-dataset: computed sha256 = $$actual_sha — pin this into the Makefile's DATASET_SHA256 constant deliberately, then re-run."; \
		exit 1; \
	elif [ "$$actual_sha" != "$(DATASET_SHA256)" ]; then \
		echo "eval-dataset: SHA256 MISMATCH — expected $(DATASET_SHA256), got $$actual_sha. Refusing to use a dataset that does not match the pinned hash." >&2; \
		exit 1; \
	else \
		echo "eval-dataset: sha256 verified ($$actual_sha)"; \
	fi

# --------------------------------------------------------------------------------------------
# Isolated eval box lifecycle

eval-up:
	@mkdir -p eval/out/panella-local
	@if [ ! -f eval/out/compose.env ]; then \
		echo "eval-up: minting a scratch PANELLA_API_KEY into eval/out/compose.env (gitignored, fresh key every eval-up)"; \
		api_key=$$(openssl rand -hex 32 2>/dev/null || $(PYTHON) -c 'import secrets; print(secrets.token_hex(32))'); \
		printf 'PANELLA_API_KEY=%s\n' "$$api_key" > eval/out/compose.env; \
	fi
	$(EVAL_COMPOSE) build
	$(EVAL_COMPOSE) up -d --wait --wait-timeout 300
	@echo "eval-up: minting an owner bearer THROUGH THE SAME WRAPPER (no panella-init, no bare docker exec — see brief §'Bearer acquisition')"
	@ts=$$(date -u +%Y%m%dT%H%M%SZ); \
	root_id=$$($(EVAL_COMPOSE) exec -T panella-http python3 -c "from panella.principal import root_principal; print(root_principal().id)" | tr -d '\r'); \
	bearer=$$($(EVAL_COMPOSE) exec -T panella-http panella tokens mint --principal "$$root_id" --label "eval-owner-$$ts" 2>/dev/null | tail -n1 | tr -d '\r'); \
	if [ -z "$$bearer" ]; then \
		echo "eval-up: FAILED to mint a bearer token — check '$(EVAL_COMPOSE) logs panella-http'" >&2; \
		exit 1; \
	fi; \
	printf 'PANELLA_EVAL_BEARER=%s\n' "$$bearer" > eval/out/state.env; \
	printf 'PANELLA_EVAL_STORE_URL=http://127.0.0.1:18000\n' >> eval/out/state.env; \
	printf 'PANELLA_EVAL_FACADE_URL=http://127.0.0.1:18001\n' >> eval/out/state.env; \
	echo "eval-up: wrote eval/out/state.env (bearer minted, not printed here)"
	@echo "eval-up: box is up. Store: http://127.0.0.1:18000  Facade: http://127.0.0.1:18001"

eval-down:
	$(EVAL_COMPOSE) down -v
	@rm -rf eval/out/panella-local
	@rm -f eval/out/state.env eval/out/compose.env
	@echo "eval-down: torn down (volumes via -v; per-box scratch state.env/compose.env removed — a stale bearer for deleted volumes must not make eval-selftest treat the box as up)"

# --------------------------------------------------------------------------------------------
# Pipeline (requires eval-up + eval-dataset)

eval-retrieve: eval-selftest
	@set -a; . eval/out/compose.env; . eval/out/state.env; set +a; \
	PANELLA_EVAL_API_KEY="$$PANELLA_API_KEY" $(PYTHON) -m eval.longmemeval.ingest_retrieve \
		--lane store --data "$(DATASET_FILE)" --out eval/out/stage_a_retrieval.store.json
	@set -a; . eval/out/compose.env; . eval/out/state.env; set +a; \
	PANELLA_EVAL_API_KEY="$$PANELLA_API_KEY" $(PYTHON) -m eval.longmemeval.ingest_retrieve \
		--lane facade --data "$(DATASET_FILE)" --out eval/out/stage_a_retrieval.facade.json
	$(PYTHON) -m eval.longmemeval.compare_lanes \
		--store eval/out/stage_a_retrieval.store.json --facade eval/out/stage_a_retrieval.facade.json \
		--out eval/out/lane_comparison.json

eval-qa:
	$(PYTHON) -m eval.longmemeval.qa --retr eval/out/stage_a_retrieval.facade.json --out eval/out/stage_a_qa.json

eval-report:
	$(PYTHON) -m eval.render_report \
		--lane-comparison eval/out/lane_comparison.json \
		--qa eval/out/stage_a_qa.json \
		--key-correctness eval/out/key_correctness_report.json \
		--supersede-report eval/out/supersede_report.json \
		--dataset-name "$(DATASET_NAME)" \
		--dataset-sha256 "$(DATASET_SHA256)" \
		--panella-commit "$$(git rev-parse --short=12 HEAD)" \
		--compose-project panella-eval \
		--out eval/out/report.md
	@echo "eval-report: wrote eval/out/report.md (numbers land ONLY under eval/out/, never printed here)"

# --------------------------------------------------------------------------------------------
# Smoke (n=2/type, both lanes end-to-end) -- machine-readable status, never faked

eval-smoke: eval-selftest
	@mkdir -p eval/out
	@cp eval/tests/fixtures/smoke_dataset.json eval/out/smoke_fixture.json
	$(PYTHON) eval/smoke.py


# --------------------------------------------------------------------------------------------
# Visibility canary -- runs the make-or-break check against the LIVE eval box on its own (the
# canary is not skippable inside ingest_retrieve.py's --lane facade path; this target lets an
# operator/CI re-run JUST the canary without a full retrieval pass). Requires `make eval-up` to
# have already minted eval/out/state.env -- if it hasn't, this target fails loudly (it does NOT
# silently skip; skipping is eval-selftest's job when no box is up, see below).

eval-visibility-canary:
	@if [ ! -f eval/out/state.env ]; then \
		echo "eval-visibility-canary: eval/out/state.env missing -- run 'make eval-up' first (this target requires a live eval box, it does not skip)" >&2; \
		exit 1; \
	fi
	@set -a; . eval/out/compose.env; . eval/out/state.env; set +a; \
	PANELLA_EVAL_API_KEY="$$PANELLA_API_KEY" $(PYTHON) -m eval.longmemeval.ingest_retrieve \
		--lane facade --canary-only

# --------------------------------------------------------------------------------------------
# Self-test: unit tests + isolation proof + visibility canary IFF a live box is up (eval/out/state.env
# exists, i.e. `make eval-up` has run) -- when no box is up, this prints an explicit SKIPPED line
# rather than silently omitting the canary (a comment here promising a skip-with-notice, and no
# other behavior, is the only way this target is allowed to not run the canary).

eval-selftest: eval-isolation-check
	ruff check eval/
	$(PYTHON) -m pytest eval/tests -q
	$(PYTHON) eval/goldsets/synth_supersede.py --check
	@if [ -f eval/out/state.env ]; then \
		$(MAKE) eval-visibility-canary; \
	else \
		echo "canary: SKIPPED (no live eval box)"; \
	fi

eval-isolation-check:
	@echo "eval-isolation-check: asserting the eval compose project is fully eval-scoped (mechanical proof, not prose)"
	@mkdir -p eval/out
	@if [ ! -f eval/out/compose.env ]; then \
		api_key=$$(openssl rand -hex 32 2>/dev/null || $(PYTHON) -c 'import secrets; print(secrets.token_hex(32))'); \
		printf 'PANELLA_API_KEY=%s\n' "$$api_key" > eval/out/compose.env; \
	fi
	@config_json=$$($(EVAL_COMPOSE) config --format json); \
	echo "$$config_json" | $(PYTHON) -c "\
import json, sys; \
c = json.load(sys.stdin); \
assert c['name'] == 'panella-eval', f\"project name is {c['name']!r}, expected 'panella-eval'\"; \
store_ports = c['services']['panella']['ports']; \
facade_ports = c['services']['panella-http']['ports']; \
assert len(store_ports) == 1 and store_ports[0]['published'] == '18000', f'store ports={store_ports}'; \
assert len(facade_ports) == 1 and facade_ports[0]['published'] == '18001', f'facade ports={facade_ports}'; \
mounts = {v['target']: v.get('source', '') for v in c['services']['panella-http']['volumes']}; \
assert mounts.get('/app/local', '').endswith('eval/out/panella-local'), f\"facade /app/local source={mounts.get('/app/local')!r}, expected the eval scratch dir\"; \
env = c['services']['panella-http']['environment']; \
assert env.get('PANELLA_GOVERNANCE_OVERLAY', '') == '', f\"PANELLA_GOVERNANCE_OVERLAY={env.get('PANELLA_GOVERNANCE_OVERLAY')!r}, expected cleared\"; \
print('eval-isolation-check: PASS — project=panella-eval, ports=18000/18001, /app/local=eval scratch, overlay=cleared')"

# --------------------------------------------------------------------------------------------
# Public-number gate — greps TRACKED files for metric-looking patterns, exits non-zero on hits.
# Runs against `git ls-files` (tracked tree only) so eval/out/ (gitignored, numeric by design) is
# never scanned — this gate is about what SHIPS in the repo, not about the scratch working state.

eval-public-scan:
	$(PYTHON) eval/public_scan.py
