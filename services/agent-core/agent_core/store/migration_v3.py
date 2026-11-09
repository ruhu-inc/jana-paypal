"""Immutable M21 profile-local captures and editor drafts migration (DAT-12, D68)."""

LEARNER = (
    "-- Input capture records and editor drafts (M21, D68): "
    "profile-local learner data, no original photo or audio.\n"
    """CREATE TABLE captures (
  capture_id TEXT PRIMARY KEY CHECK (capture_id GLOB 'cap_[0-9A-Z]*' AND length(capture_id) = 30),
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  record TEXT NOT NULL CHECK (json_valid(record)),
  expires_at INTEGER NOT NULL
) STRICT;
CREATE INDEX captures_expiry ON captures(expires_at);
CREATE TABLE editor_drafts (
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  slot TEXT NOT NULL CHECK (length(slot) <= 96),
  document TEXT NOT NULL CHECK (json_valid(document) AND length(document) <= 32768),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, slot)
) STRICT;
"""
)
