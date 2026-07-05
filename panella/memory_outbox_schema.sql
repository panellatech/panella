-- /home/owner/panella/data/memory_outbox.db
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  worker_id TEXT,
  claimed_at TEXT,
  processing_started_at TEXT,
  attempt_count INTEGER DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  processed_at TEXT,
  completed_memory_id TEXT,
  shadow INTEGER NOT NULL DEFAULT 0,
  -- No identity DEFAULT: the single write path (client_raw._approve_in_conn) always supplies
  -- tenant_id explicitly. (The client_raw ALTER-backfill for pre-existing DBs keeps its literal
  -- default — SQLite requires one to ALTER a NOT NULL column onto a non-empty table.)
  tenant_id TEXT NOT NULL,
  principal_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status ON memory_events(status);
CREATE INDEX IF NOT EXISTS idx_events_claimed ON memory_events(status, claimed_at);
CREATE INDEX IF NOT EXISTS idx_events_retry ON memory_events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_events_shadow ON memory_events(shadow);

CREATE VIEW IF NOT EXISTS outbox AS SELECT * FROM memory_events;

CREATE TABLE IF NOT EXISTS approval_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  tg_message_id INTEGER,           -- latest bot-sent message id (edit-in-place target)
  tg_message_ids TEXT,             -- JSON array of ALL message ids sent for this approval (resends)
  tg_chat_id TEXT,
  tg_last_sent_at TEXT,
  memory_event_id INTEGER,
  decided_by TEXT,
  created_at TEXT NOT NULL,
  responded_at TEXT,
  expires_at TEXT,
  last_error TEXT,
  -- Stage 2 P0 — handler-authorized approval provenance (set ONLY by
  -- approve_authorized_telegram_candidate, never by raw approve_queued_candidate),
  -- so the finalizer can fail-closed on a forged status='approved' DB row.
  approved_via TEXT,                  -- 'telegram' when stamped by the authenticated handler
  approved_by TEXT,                   -- 'telegram:<verified presser_id>'
  approved_tg_message_id INTEGER,     -- the bot-sent button message the approval was bound to
  -- Stage 2 P0 — durable-finalization state machine (separate from status).
  finalizer_state TEXT,               -- NULL/'none' -> 'finalizing' -> 'finalized'/'failed'
  finalizer_worker_id TEXT,           -- claim owner; record/fail CAS on this
  finalizer_claimed_at TEXT,          -- claim time; stale-reclaim after STALE_TTL
  finalizer_attempt_count INTEGER NOT NULL DEFAULT 0,
  durable_memory_id TEXT,             -- authoritative upstream content_hash of the durable row
  supersede_target_id TEXT,           -- P2 only (NULL in P0)
  supersede_done_at TEXT,             -- P2 only (NULL in P0)
  finalizer_last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_queue(status);
CREATE INDEX IF NOT EXISTS idx_approval_expires ON approval_queue(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_approval_tg_last_sent ON approval_queue(status, tg_last_sent_at);

CREATE TABLE IF NOT EXISTS telegram_poll_state (
  bot_name TEXT PRIMARY KEY,
  last_update_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- Phase 1.5 (item E): Panella-owned append-only audit log of memory state transitions.
-- Append-only by construction (INSERT only; no UPDATE/DELETE). Best-effort writer.
-- op is a SUPERSET of WriteResult.op: stored|dedup_skipped|queued_for_approval (writes)
--   + supersede|tombstone|hard_delete (transitions). 6 canonical values; pinned by test.
CREATE TABLE IF NOT EXISTS memory_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_iso          TEXT NOT NULL,
  op              TEXT NOT NULL,
  drawer_id       TEXT NOT NULL,            -- == upstream content_hash; the reconciliation join key
  tenant_id       TEXT NOT NULL,            -- acting principal's tenant ('*' on break-glass hard_delete)
  principal_id    TEXT,                     -- acting principal (who performed the op)
  wing            TEXT,                     -- populated on write; NULL on transitions
  room            TEXT,
  author_agent_id TEXT,                     -- caller-asserted author (write path; from payload_metadata)
  source_bridge   TEXT,
  session_id      TEXT,
  reason          TEXT,                     -- transition reason (supersede/tombstone/hard_delete)
  superseded_by   TEXT,                     -- supersede only; expected-NULL until a Phase-2 caller supplies it
  details_json    TEXT                      -- nullable escape hatch (future fields; avoids append-only ALTER)
);
CREATE INDEX IF NOT EXISTS idx_history_drawer ON memory_history(drawer_id);
CREATE INDEX IF NOT EXISTS idx_history_ts ON memory_history(ts_iso);
