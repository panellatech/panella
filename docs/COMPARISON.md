# How Panella compares

**Honest summary up front: if you want a personal assistant that quietly gets smarter, several
excellent memory layers do that well — and Panella is not trying to be one.** Panella is for teams
and companies that need to answer *"on whose authority is this true?"* with evidence.

## The two branches

Agent memory systems today make one foundational choice: what happens between an agent writing
something and it becoming durable truth?

**Branch one — automatic consolidation.** Most of the field takes this branch: memories are
extracted, merged, summarized, and updated in the background, automatically. It optimizes for
speed and zero ceremony, and for a single-user assistant that is a reasonable, deliberate design.

**Branch two — governed writes.** Panella takes the other branch: an agent's write to a governed
scope can only ever *propose*. The proposal queues, an authorized approver — a separate,
operator-held credential, not the agent's own — signs it off, and the decision itself is kept as a
chain-verified receipt the system can later prove. Nothing an agent submits becomes durable truth
without that receipt. (The shipped approval transport records the canonical operator identity; if
several people share that credential, the receipt proves the authority, not which teammate — give
approvers distinct credentials when that distinction matters.)

The branches are not interchangeable. Retrofitting governance onto an auto-consolidating core means
governing what already happened; Panella gates the write itself.

## Where the field is converging — and where it isn't

Category observations, current as of mid-2026 (no vendor named; check any product's own docs
against these patterns):

1. **Audit is becoming table stakes — audit *after the write*.** Managed memory platforms
   increasingly ship audit logs, retention policies, and access control. These record what
   happened; they do not gate whether it happens. The write itself still lands first.
2. **Governance features tend to live in managed or enterprise tiers.** Access control, audit,
   and compliance tooling typically arrive with the paid platform, sales conversation, or cloud
   deployment — not in the free self-hosted core.
3. **Teams hand-roll the gap.** Teams running popular memory layers commonly build their own
   approval, audit, and conflict-resolution layers on top. The need is real; it is just not the
   default anywhere.
4. **Benchmarks are vendor-run.** Published accuracy numbers in this category overwhelmingly come
   from each vendor's own harness. They are not comparable across products, and chasing them
   rewards the ungoverned fast path.

## What Panella does differently

Every row below is a shipped, verifiable property of the open Apache-2.0 box — not a roadmap item
and not an enterprise tier.

| Dimension | Common pattern in the field | Panella |
|---|---|---|
| Write path | Agent writes land, then are consolidated automatically | Agent MCP writes to **governed scopes are propose-only by construction**; a person approves before durability. The shipped agent profiles are governed end to end; ungoverned scopes arise only from a deployment's own profile configuration — audit any custom profile |
| Approval identity | Caller's API key implies authority | **Two separate credentials**: the agent bearer can only propose; an operator-held approval token is the approver identity — kept outside the agent's sandbox or OS user, so an agent cannot approve its own memory |
| Audit | Append-only log of what happened | **Chain-verified approval receipts**: the finalizer refuses to make a write durable without a receipt it can verify — whoever stamped the row |
| Where governance lives | Managed platform / enterprise tier | **In the free, self-hosted core, by default** — governance is never a paid feature |
| Background consolidation | Core feature | **Never.** Original content is immutable; updates version, never overwrite |
| Deployment | Cloud-first, self-host varies | **Self-host-first**: one command (`panella up`) brings up a digest-pinned, cosign-signed, offline-friendly box on your hardware |
| Ambiguity handling | Best-effort merge | **Deny-closed**: unknown or ambiguous cases fall back to proposal-only, never silent auto-write |
| Benchmark numbers | Vendor-published leaderboards | **None published, by design.** A reproducible eval bundle ships in-repo; run it on your own hardware and get your own numbers |

## What Panella does not claim

- **Not the fastest way to a smarter personal assistant.** Approval is ceremony; ceremony is the
  point. For a single-user assistant, branch one is a fine choice.
- **No leaderboard entry.** The in-repo [eval bundle](https://github.com/panellatech/panella/blob/main/eval/README.md)
  exists to let you check one thing on your own hardware — that the governed read path does not
  cost you recall versus the raw store path. It deliberately publishes no numbers; numbers you
  produce yourself are the only kind this category should trust.
- **Not a chat-indexing or knowledge-ingestion engine.** Panella stores what someone decided is
  worth remembering and approved — not everything that scrolled past.

## If you're evaluating

Ask any memory product — including us — these questions:

1. Can an agent's write become durable truth without a human decision? Under what configuration?
2. Is the approval identity a *separate credential* from the agent's API access, or does bearer
   access imply approval power?
3. When someone asks "who approved this fact?", is the answer a provable record or a log line?
4. Is governance in the free core, or does it arrive with the enterprise tier?
5. Can you re-run the vendor's accuracy claims on your own hardware?

Panella's answers: on the governed path, never — and whether a scope is governed is decided by the
deployment's profile configuration (the shipped profiles are governed; audit your custom ones);
separate by construction, with the approval token kept outside the agent's sandbox or OS user; a
chain-verified receipt; free core, always; yes — the in-repo bundle walks it:
`make eval-up && make eval-dataset && make eval-retrieve`
([eval/README.md](https://github.com/panellatech/panella/blob/main/eval/README.md)).

---

- [Governance model](https://github.com/panellatech/panella/blob/main/docs/GOVERNANCE.md) ·
  [Quickstart](https://github.com/panellatech/panella/blob/main/docs/QUICKSTART.md) ·
  [Security policy](https://github.com/panellatech/panella/blob/main/SECURITY.md)
