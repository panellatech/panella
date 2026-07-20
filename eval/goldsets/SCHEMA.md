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

**`coexist` vs `unrelated` — the discriminator.** Both labels keep both facts in `current_truth`,
so the boundary is about RELATEDNESS, not survival. `coexist` is deliberately narrow: it marks a
pair the case presents as candidate related-aspects — facts a naive updater might wrongly treat as
a slot update of one another (the anti-false-supersede probe). Any pair of merely independent
attributes of the user — different slots, no shared aspect cluster — is `unrelated`, even though
both facts are simultaneously true of the same person (the anti-false-merge trap; "same user" is
never by itself a shared subject, otherwise every pair in a personal-memory goldset would share
one). When genuinely torn between `coexist` and `unrelated`, the label is `unrelated`.

## `high_risk` (optional, v1+)

A pair may carry an optional `"high_risk": true` flag (default/absent = false) when at least one
fact in the pair concerns a high-risk/sensitive slot — medication, legal name, emergency contact,
home-address-sharing, primary physician, dietary restriction. It is orthogonal to `label`: an
`high_risk` pair can be `supersede` (a genuine same-slot update over a sensitive attribute, e.g. a
medication change) or `unrelated` (a sensitive fact paired with a later benign fact that must NOT be
merged into it). `score_supersede.py` reports a dedicated hr-slice (`hr_supersede_recall`,
`hr_false_merge_count`, `hr_coverage`) over exactly these pairs, because a false merge here is a
higher-consequence failure than an ordinary one.

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

### `high_risk` — `supersede` positive

```json
{
  "earlier_id": "f-earlier",
  "later_id": "f-later",
  "label": "supersede",
  "high_risk": true
}
```
Facts: `f-earlier` = "takes Veltrazine daily for hypertension"; `f-later` = "switched to Norvexol
last month" (both fictional drug names). Same slot (current medication), later fact wins, AND the
slot is sensitive — flagged `high_risk` so `score_supersede.py`'s hr-slice tracks it separately.

### `high_risk` — `unrelated` positive (the deadliest trap)

```json
{
  "earlier_id": "f-a",
  "later_id": "f-b",
  "label": "unrelated",
  "high_risk": true
}
```
Facts: `f-a` = "is allergic to shellfish"; `f-b` = "switched to the Quill Notes note-taking app". No
shared slot — a classifier that merges a sensitive fact into an unrelated later fact just because it
is more recent (or "looks important") produces exactly this false merge, now on a high-risk value.

## Determinism

`synth_supersede.py --check` asserts the on-disk `supersede_v1.json` is byte-identical to a fresh
regeneration from the fixed seed, that it validates against `supersede.schema.json`, AND that it
clears the hard content bars in `synth_supersede.py`'s `_check_bars` (total cases/pairs, per-label
pair counts, hr-flagged pair counts, coexist-trap case count — see that module's docstring for the
exact numbers). Cases are ordered by `case_id`; pairs within a case are ordered by
`(earlier_id, later_id)`.

Additionally, every template value carries a life-domain `aspect` tag (a closed vocabulary inside
`synth_supersede.py`, exported as the `CONTENT_META` map), and the generator mechanically enforces
ASPECT DISJOINTNESS for `unrelated` pairs: multi-fact draws that would label two same-aspect (or
same-slot, or identical-content) facts `unrelated` are deterministically rejected and redrawn, and
a post-generation sweep (`_check_aspect_disjointness`, also part of `--check`) asserts over the
entire goldset that every `unrelated` pair joins two facts with different contents, different
source slots, and different aspects — while `coexist` pairs need only different slots (sharing an
aspect is what coexist means; see the discriminator note above).
