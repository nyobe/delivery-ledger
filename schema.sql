-- The ledger: one append-only table, two fact classes, every row attributed.
--
-- Everything else in this repo is a query over this table. If a view ever
-- needs a second mutable table to render, that is a design smell — record it
-- in smells.md rather than adding the table.

CREATE TABLE facts (
  seq       INTEGER PRIMARY KEY,           -- arrival order; the ledger is append-only
  id        TEXT    NOT NULL UNIQUE,       -- stable handle so facts can cite facts
  ts        TEXT    NOT NULL,              -- ISO-8601 UTC
  class     TEXT    NOT NULL CHECK (class IN ('intent', 'observation')),
  kind      TEXT    NOT NULL,              -- e.g. promotion.decided, transition.finished
  subject   TEXT    NOT NULL,              -- '<type>:<name>' — stage:, freight:, transition:, edge:, record:
  actor     TEXT    NOT NULL,              -- who/what wrote it: user:, policy:, cron:, ci:, watch:
  payload   TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
  rationale TEXT,                          -- why (intent facts)
  refs      TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(refs))  -- fact ids this one answers to
);

CREATE INDEX facts_subject      ON facts (subject, seq);
CREATE INDEX facts_kind_subject ON facts (kind, subject, seq);

-- The only non-ledger state: what time it is. Views that ask "active now?"
-- (holds) read it. The renderer sets it so the fixture renders the same at any
-- wall-clock time.
CREATE TABLE clock (now TEXT NOT NULL);
