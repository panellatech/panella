# Panella defensive-parity eval report

Template only -- `make eval-report` renders this into `eval/out/report.md` with the run's actual
numbers filled in. No numbers are committed in this template file itself.

This bundle exists to show governed reads do not cost recall -- it is not a leaderboard entry.

## Run stamp

| Field | Value |
| --- | --- |
| Dataset | `{{DATASET_NAME}}` (`{{DATASET_SHA256}}`) |
| Panella version / commit | `{{PANELLA_COMMIT}}` |
| Eval box config | project `{{COMPOSE_PROJECT}}`, ports `{{STORE_PORT}}`/`{{FACADE_PORT}}` |
| `PANELLA_HTTP_PROFILE` | `{{HTTP_PROFILE}}` |
| Run started (UTC) | `{{RUN_STARTED_AT}}` |
| Subset size (n per question type) | `{{N_PER_TYPE}}` |

## Retrieval recall@k -- store lane vs facade lane

| Question type | n (store) | n (facade) | recall@1 (store) | recall@1 (facade) | delta | recall@5 (store) | recall@5 (facade) | delta | recall@10 (store) | recall@10 (facade) | delta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| {{PER_TYPE_ROWS}} |
| **OVERALL** | {{OVERALL_STORE_N}} | {{OVERALL_FACADE_N}} | {{OVERALL_STORE_R1}} | {{OVERALL_FACADE_R1}} | {{OVERALL_DELTA_R1}} | {{OVERALL_STORE_R5}} | {{OVERALL_FACADE_R5}} | {{OVERALL_DELTA_R5}} | {{OVERALL_STORE_R10}} | {{OVERALL_FACADE_R10}} | {{OVERALL_DELTA_R10}} |

## Intentional lane deltas (locked framing -- always emitted, never cherry-picked)

The two lanes are not the same ranking function. The facade path adds the governance surface a
real user actually runs through; the store lane is the raw baseline. Every semantic difference,
with its actual config value in THIS run:

| Delta | Shipped default (this run) | Effect |
| --- | --- | --- |
| {{INTENTIONAL_LANE_DELTAS_ROWS}} |

## QA-accuracy (if run)

| Question type | n | accuracy |
| --- | --- | --- |
| {{QA_PER_TYPE_ROWS}} |
| **OVERALL** | {{QA_OVERALL_N}} | {{QA_OVERALL_ACC}} |

Reader: `{{READER_MODEL}}` (`{{READER_TRANSPORT}}`) -- Judge: `{{JUDGE_MODEL}}` (`{{JUDGE_TRANSPORT}}`)
-- Reader budget: top-`{{READER_K}}`

## key_correctness (K0 goldset -- `eval/goldsets/key_correctness_eval.py`)

| Metric | Value | Bar |
| --- | --- | --- |
| schema_validity | {{KC_SCHEMA_VALIDITY}} | 1.0 |
| key_correctness | {{KC_KEY_CORRECTNESS}} | -- |
| key_stability | {{KC_KEY_STABILITY}} | >=0.90 |
| supersede_precision | {{KC_SUPERSEDE_PRECISION}} | >=0.95 |
| harmful_collisions | {{KC_HARMFUL_COLLISIONS}} | 0 |
| negative_false_positive_rate | {{KC_NEG_FP_RATE}} | 0 |
| **Verdict** | **{{KC_VERDICT}}** | -- |

## Supersede confusion-matrix (`eval/goldsets/score_supersede.py`, goldset v0)

| Label | Precision | Recall |
| --- | --- | --- |
| supersede | {{SUP_PRECISION_SUPERSEDE}} | {{SUP_RECALL_SUPERSEDE}} |
| coexist | {{SUP_PRECISION_COEXIST}} | {{SUP_RECALL_COEXIST}} |
| unrelated | {{SUP_PRECISION_UNRELATED}} | {{SUP_RECALL_UNRELATED}} |

false_merge_count: `{{SUP_FALSE_MERGE_COUNT}}` (predicted supersede/coexist where gold says
unrelated -- the dangerous cross-slot confusion)

coverage: `{{SUP_COVERAGE}}` (predicted pairs / gold pairs -- a gold pair with NO matching
prediction counts against recall on its label AND deflates this separately; a missing pair is
never scored as a vacuous pass)

## Dataset & reproduction

Reproduce this exact run: `eval/README.md`. Dataset download + sha256 verification:
`make eval-dataset`. Full pipeline: `make eval-retrieve eval-qa eval-report`.
