"""Personal tutor memory online and check-ins (SPEC §19.3.4, §19.3.5, W8 phase 3).

Learner database, v21:
- `settings` admits `checkin_notifications` (a JSON boolean, default false; JM-D24) and `consents` the scope
  `tutor_memory_online` (JM-D21); only the `scope` column's own list is edited (its first occurrence), never the
  method CHECKs below it that name the research scopes. Both CHECKs only widen, so every stored row still satisfies
  them; the stored
  definitions are edited in place and the schema version bumped, as `migration_v19` does for `sessions` (a rebuild
  through a renamed copy would cascade deletes through the tables that reference the old one).
- `companion_episodes.checkin_id`: the check-in an episode was opened for; its first accepted turn answers it.
- `commitments`: the check-ins (§19.3.4), device-local (JM-D27), purged 30 days after resolution.
- `note_recalls`: one row per note, session and day the tutor recalled it (§19.3.5), device-local, kept as long as
  notes are and removed with the note. No foreign key: the notes projection may rebuild `notes`, and a cascade would
  take the recall counts with it.

Device database, v3: `online_features` admits `tutor_memory_online`, the same way."""

LEARNER = """
PRAGMA writable_schema = ON;
UPDATE sqlite_schema
   SET sql = replace(replace(sql,
       '''faster_tutor'',''wake_word''))', '''faster_tutor'',''wake_word'',''checkin_notifications''))'),
       'AND (key <> ''wake_word'' OR value IN (''true'',''false'')))',
       'AND (key <> ''wake_word'' OR value IN (''true'',''false''))
                     AND (key <> ''checkin_notifications'' OR value IN (''true'',''false'')))')
 WHERE type = 'table' AND name = 'settings' AND instr(sql, '''checkin_notifications''') = 0;
UPDATE sqlite_schema
   SET sql = substr(sql, 1, instr(sql, '''model_training_research'')),') - 1)
             || '''model_training_research'',''tutor_memory_online'')),'
             || substr(sql, instr(sql, '''model_training_research'')),') + length('''model_training_research'')),'))
 WHERE type = 'table' AND name = 'consents' AND instr(sql, '''tutor_memory_online''') = 0;
PRAGMA writable_schema = OFF;
ALTER TABLE companion_episodes ADD COLUMN checkin_id TEXT
  CHECK (checkin_id IS NULL OR (checkin_id GLOB 'cmt_*' AND length(checkin_id) = 30));
CREATE TABLE commitments (
  commitment_id TEXT   PRIMARY KEY CHECK (commitment_id GLOB 'cmt_*' AND length(commitment_id) = 30),
  kind         TEXT    NOT NULL CHECK (kind IN ('event_checkin','deadline_check','open_loop','due_reviews',
                                               'pack_ready')),
  subject      TEXT    CHECK (subject IS NULL OR subject IN ('mathematics','physics','chemistry','biology',
                                                             'accounting')),
  kc_id        TEXT,
  text         TEXT    NOT NULL CHECK (length(text) BETWEEN 1 AND 140),
  due_from     INTEGER NOT NULL,
  due_until    INTEGER NOT NULL CHECK (due_until > due_from AND due_until - due_from <= 259200000),
  dedupe_key   TEXT    NOT NULL UNIQUE CHECK (length(dedupe_key) <= 160),
  confidence   REAL    NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  state        TEXT    NOT NULL CHECK (state IN ('pending','shown','opened','dismissed','answered','expired',
                                                 'detached')),
  source       TEXT    NOT NULL CHECK (source IN ('extractor','heartbeat')),
  source_session_id TEXT,
  created_at   INTEGER NOT NULL,
  shown_at     INTEGER,
  resolved_at  INTEGER
) STRICT;
CREATE INDEX commitments_due ON commitments(state, due_from);
CREATE TABLE note_recalls (
  note_id      TEXT    NOT NULL,
  session_id   TEXT    NOT NULL,
  day          TEXT    NOT NULL CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
  PRIMARY KEY (note_id, session_id, day)
) STRICT;
"""

DEVICE = """
PRAGMA writable_schema = ON;
UPDATE sqlite_schema
   SET sql = replace(sql, '''crash_reports'')),', '''crash_reports'',''tutor_memory_online'')),')
 WHERE type = 'table' AND name = 'online_features' AND instr(sql, '''tutor_memory_online''') = 0;
PRAGMA writable_schema = OFF;
"""
