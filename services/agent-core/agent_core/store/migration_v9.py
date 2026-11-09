"""Local session-note projections and durable generation status (STU-14, UXS-52).

Deleted projections retain only their ids and revision so backlog recovery never recreates a learner-deleted note.
No transcript foreign key: notes outlive transcript retention and can be exported without exporting conversations.
"""

LEARNER = """
CREATE TABLE session_notes (
  note_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  subject TEXT,
  kc_ids TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(kc_ids)),
  statements TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(statements)),
  state TEXT NOT NULL CHECK (state IN ('queued','writing','ready','failed','no_model','deleted')),
  private INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0,1)),
  source_revision INTEGER NOT NULL,
  withheld INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX session_notes_updated ON session_notes(updated_at);
"""
