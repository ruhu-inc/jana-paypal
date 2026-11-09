"""The six tutor characters, the faster tutor and the wake word in the settings; the private mark on study notes
(DAT-12; D103, D104).

- The settings table's CHECK names every key of the canonical student-preference set, so it is rebuilt with
  `faster_tutor` and `wake_word` (JSON booleans; UXS-03, MDL-05, VOC-49) and with `tutor_avatar` held to the six
  character ids of TABLE DSN-26a. A stored avatar outside the set (the earlier `avatar.default` and `avatar.disc_N`,
  G119) is rewritten to `avatar.jana` as the row is copied; every other row is kept as it was.
- `study_notes.private` (STU-15, D102): the student's mark, 0 until they set it. The marks a core before API 1.10 kept
  in a side file beside the database are moved into the column when the notes are first read (`api.study_notes`)."""

CHARACTERS = ("avatar.jana", "avatar.orbit", "avatar.prism", "avatar.kite", "avatar.pebble", "avatar.ray")
DEFAULT_CHARACTER = "avatar.jana"

LEARNER = """
ALTER TABLE settings RENAME TO previous_settings;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('low_power','text_scale','locale',
                     'memory_saving','notifications','reduced_motion','read_aloud','easy_read','extra_time_percent','speech_rate','speech_voice',
                     'theme','character_shortcuts','tutor_name','tutor_avatar','faster_tutor','wake_word')),
  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'extra_time_percent' OR value IN ('0','25','50'))
                     AND (key <> 'theme' OR value IN ('"system"','"light"','"dark"'))
                     AND (key <> 'character_shortcuts' OR value IN ('true','false'))
                     AND (key <> 'tutor_name' OR (json_type(value) = 'text'
                          AND length(json_extract(value, '$')) BETWEEN 1 AND 40))
                     AND (key <> 'tutor_avatar' OR value IN ('"avatar.jana"','"avatar.orbit"','"avatar.prism"',
                          '"avatar.kite"','"avatar.pebble"','"avatar.ray"'))
                     AND (key <> 'faster_tutor' OR value IN ('true','false'))
                     AND (key <> 'wake_word' OR value IN ('true','false'))),
  source           TEXT    NOT NULL CHECK (source IN ('default','student')),
  set_by_event_id  TEXT,
  hlc              TEXT    NOT NULL,
  updated_at       INTEGER NOT NULL
) STRICT;
INSERT INTO settings SELECT key,
  CASE WHEN key = 'tutor_avatar' AND value NOT IN ('"avatar.jana"','"avatar.orbit"','"avatar.prism"','"avatar.kite"',
    '"avatar.pebble"','"avatar.ray"') THEN '"avatar.jana"' ELSE value END,
  source, set_by_event_id, hlc, updated_at FROM previous_settings;
DROP TABLE previous_settings;
ALTER TABLE study_notes ADD COLUMN private INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0,1));
"""
