# Upgrading Panella

An upgrade is a version bump — the image pin or the installed package version — plus a restart.
The one rule: **back up first.** Everything below assumes the compose topology in
[SELF_HOST.md](SELF_HOST.md) (a `panella-store` volume mounted read-only into the facade, plus a
`panella-http-data` volume for the facade's own token/audit/outbox state).

## Step 0 — Backup

```bash
panella backup --out ./panella-backup-$(date +%Y%m%d).tar.gz
```

This snapshots the store, the facade's token/audit/outbox DBs, and (if configured) the governance
overlay file into one hash-manifested archive. It validates the snapshot before packaging (the
store via `store_probe`, the audit log via its hash-chain verify) and refuses to write a backup it
can't already prove is coherent. Do this **before** touching the image pin or the package version —
if anything below goes wrong, [Restore from backup](#restore-from-backup-the-rollback-story) is the
rollback.

Run it from wherever the box's data actually lives: on the host if you're going container-to-host,
or `docker compose exec panella-http panella backup --out /app/data/backup.tar.gz` inside the
facade container, then copy the archive out.

## Step 1 — Bump the pin

Edit whichever of these applies to your deployment:

- **Docker image** — bump the tag pinned in your `docker-compose.yml` / `Dockerfile` (or the
  registry tag you pull), then:

  ```bash
  docker compose pull   # or: docker compose build
  docker compose up -d
  ```

- **Package install** — bump the `panella` version in your `pyproject.toml` / `requirements.txt`,
  then reinstall (`pip install -U panella` or your project's equivalent) and restart the
  `panella-http` process.

## Step 2 — Verify

```bash
curl -sf http://127.0.0.1:8001/v1/health
```

`/v1/health` must return `200`. If you've already run `panella init`, also re-verify the
provisioning:

```bash
panella init --verify
```

If either check fails, do not keep serving on the new version — go to
[Restore from backup](#restore-from-backup-the-rollback-story).

## 10.31.2 → 11.x: majors are a deliberate migration, not a hand bump

The store contract (the SQLite schema, the tag/metadata shape the facade reads) is **pinned** — see
the OpenAPI-fixture contract test (`tests/test_store_probe_adapter_contract.py`) that locks the
`memories` table shape this facade depends on. A major version bump of the underlying store is not
a routine patch: it is a maintainer-driven migration, tracked publicly in this repo's issue tracker
(the issue #3 pattern) with its own compatibility notes and a rehearsed upgrade path.

**Self-hosters should not hand-bump a major version pin.** Wait for a tagged Panella release that
explicitly supports the new store major, follow that release's own upgrade notes, and treat this
file's Step 0–2 as the wrapper around it (backup first, bump, verify) — not a substitute for
reading what changed.

## Restore from backup (the rollback story)

```bash
panella restore --from ./panella-backup-YYYYMMDD.tar.gz --data-dir ./restored-data
```

`restore` verifies every file's sha256 against the backup's manifest **before** placing anything
on disk, refuses to overwrite existing files unless you pass `--force`, and re-runs the same
store/audit integrity checks after placing the files — so you get a pass/fail line per file, not a
silent "probably fine."

To roll back a bad upgrade:

1. Stop the stack: `docker compose down`.
2. Restore the pre-upgrade backup into the data directories the compose volumes back onto (or a
   fresh directory you then point the volumes at).
3. Revert the image tag / package version pin to the pre-upgrade value.
4. `docker compose up -d` and re-run [Step 2 — Verify](#step-2--verify).

A restore's target files are the same four roles a backup captures: `store_db`, `token_db`,
`audit_db`, `outbox_db` (plus `governance_overlay` when configured) — whichever of these existed
at backup time.
