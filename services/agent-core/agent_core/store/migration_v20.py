"""The Study companion's thread (SPEC §19.3.3, W8 phase 2): its daily episodes, one per profile day (UNIQUE
profile_day, so concurrent opens return one row), and its rolling summary, written by code only and kept in its own
table, never in `sessions.summary` (which a forget rewrites as plain text, review B9). Device-local (JM-D27)."""

LEARNER = """
CREATE TABLE companion_episodes (
  session_id   TEXT    PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
  thread_id    TEXT    NOT NULL CHECK (thread_id GLOB 'cth_*' AND length(thread_id) = 30),
  profile_day  TEXT    NOT NULL UNIQUE CHECK (profile_day GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
  opener_json  TEXT    NOT NULL CHECK (json_valid(opener_json)),
  opened_at    INTEGER NOT NULL,
  ended_at     INTEGER
) STRICT;
CREATE TABLE companion_summary (
  thread_id    TEXT    PRIMARY KEY CHECK (thread_id GLOB 'cth_*' AND length(thread_id) = 30),
  version      INTEGER NOT NULL CHECK (version >= 0),
  cursor_seq   INTEGER NOT NULL CHECK (cursor_seq >= 0),
  updated_at   INTEGER NOT NULL,
  body_json    TEXT    NOT NULL CHECK (json_valid(body_json))
) STRICT;
"""
