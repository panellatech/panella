# Operator console

A single-page, flag-gated dashboard for the box owner: pending approvals (approve/reject), a
pending-count badge, memory search, an audit tail, and corpus stats. It is a static HTML/JS/CSS
shell served by the existing facade — no separate process, no build step, no framework.

The console shell itself carries **zero data and zero secrets**. Every panel is populated by the
page's own JavaScript calling the facade's existing `/v1/approvals`, `/v1/memory/search`,
`/v1/memory/audit`, and `/v1/memory/stats` routes with the credentials you paste into the page.

## Enable it

Off by default. Set the flag and (re)start the facade:

```bash
PANELLA_CONSOLE_ENABLED=1
```

In compose, put that in `.env` (or export it before `docker compose up`) — see the passthrough
line in `docker-compose.yml`.

## Open it

```
http://127.0.0.1:8001/console
```

`8001` is the facade's published port per `docker-compose.yml` (`ports: - "127.0.0.1:8001:8001"`).
Loopback-only by design (see "Security notes" below).

## Connect

The page asks for two secrets. Paste them into the two password fields — nothing is submitted
until you click Connect, and nothing is saved anywhere: reload the page and you paste them again.

- **Owner bearer token** — mint one with `panella tokens mint` (see `docs/QUICKSTART.md` for the
  compose-exec form). This is the same bearer your MCP client uses.
- **Approval token** — the operator-only `local_cli` credential from your approval overlay setup
  (see `docs/QUICKSTART.md`'s "arm local approval" step — the file you generated with
  `openssl rand -hex 32` and wrote to the operator token file). Required only for the pending list
  and approve/reject actions; search, audit, and stats work with the bearer alone.

## Security notes

- **Loopback only by default.** The facade publishes on `127.0.0.1`, so the console is reachable
  only from the box itself unless you deliberately change the bind. **If you re-bind beyond
  loopback, put a reverse proxy with its own authentication in front of it** — the console's own
  auth model (paste-a-bearer-into-a-password-field) assumes a trusted local operator at the
  keyboard, not an internet-facing login page.
- Both secrets live in the page's JavaScript memory only for the current tab — never
  `localStorage`, never a cookie, never the URL. Closing the tab or reloading forgets both.
- Every response the console serves carries a strict Content-Security-Policy
  (`default-src 'none'; script-src 'self'; ...`) and all rendering of stored content (approval
  previews, search hits, audit rows) uses `textContent`/`createElement` only — never `innerHTML` —
  because that content can be attacker-influenced (anything that reached the approval queue).

<!-- screenshot placeholder: docs/console-screenshot.png (add once the console has been used
     against a real box with a few pending candidates) -->
