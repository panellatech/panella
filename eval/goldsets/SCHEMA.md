# Supersede confusion-matrix goldset — human register

This is the plain-language companion to `supersede.schema.json`. It exists so a reviewer can check
a labeled pair without parsing JSON Schema: what each label MEANS, and one worked positive/negative
example per label. The construction-rung work (fact-lifecycle / current-truth resolution) baselines
against this goldset via `score_supersede.py` — read that file's docstring for the scorer contract.

## Labels

A **pair** is `(earlier_id, later_id)` — two facts from the same case, `earlier_id`'s `date` is
`<=` `later_id`'s `date`. Every pair in a case is labeled with exactly one of:

- **`supersede`** — `later_id` replaces `earlier_id` as the current truth for the SAME slot (same
  subject + attribute). An ideal system's `current_truth` for this case includes `later_id` for
  that slot, NOT `earlier_id`.
- **`coexist`** — both facts remain true simultaneously; this is not a slot-update pair (e.g. two
  independent preferences, or two facts about different attributes of the same entity). An ideal
  system's `current_truth` includes BOTH.
- **`unrelated`** — the two facts share no slot or subject at all. A classifier that merges them
  into the same tracked "fact" would be a false collision (the dangerous failure mode
  `score_supersede.py`'s confusion matrix exists to catch).

## Worked examples

### `supersede` — positive

```json
{
  "earlier_id": "employer-a",
  "later_id": "employer-b",
  "label": "supersede"
}
```
Facts: `employer-a` = "works at Northwind Traders" (2024-01-10); `employer-b` = "now works at
Contoso Labs" (2024-06-22). Same slot (current employer), later fact wins. `current_truth`
includes `employer-b`, not `employer-a`.

### `supersede` — negative (a pair that must NOT be labeled `supersede`)

```json
{
  "earlier_id": "employer-a",
  "later_id": "coffee-a",
  "label": "unrelated"
}
```
Facts: `employer-a` = "works at Northwind Traders"; `coffee-a` = "drinks black coffee, no sugar".
Different subject entirely — a classifier that treats a later-dated, unrelated fact as
"superseding" an earlier one on recency alone (ignoring subject/slot) produces exactly this false
merge. This is the `unrelated` label's reason for existing: it is the trap case, not a filler
category.

### `coexist` — positive

```json
{
  "earlier_id": "diet-a",
  "later_id": "music-a",
  "label": "coexist"
}
```
Facts: `diet-a` = "avoids gluten"; `music-a` = "enjoys ambient electronic music". Both remain true
at once — neither updates the other. `current_truth` includes both.

### `coexist` — negative (a pair that looks like `coexist` but is actually `supersede`)

```json
{
  "earlier_id": "city-a",
  "later_id": "city-b",
  "label": "supersede"
}
```
Facts: `city-a` = "lives in Rivertown" (2023-11-02); `city-b` = "moved to Lakeside last month"
(2024-03-15). A naive classifier might treat two "lives in X" statements as independently
coexisting facts (the way two unrelated preferences do); they are actually the SAME slot
(home_city) and the later one supersedes the earlier — this is the false-negative trap for
`coexist` (failing to detect a real update).

### `unrelated` — positive

```json
{
  "earlier_id": "phone-a",
  "later_id": "editor-a",
  "label": "unrelated"
}
```
Facts: `phone-a` = "phone model is a Solstice X12"; `editor-a` = "prefers the Nimbus code editor".
No shared slot or subject.

## Determinism

`synth_supersede.py --check` asserts the on-disk `supersede_v0.json` is byte-identical to a fresh
regeneration from the fixed seed, AND that it validates against `supersede.schema.json`. Cases are
ordered by `case_id`; pairs within a case are ordered by `(earlier_id, later_id)`.
