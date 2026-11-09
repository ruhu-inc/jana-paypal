"""Immutable study notes and student voice preferences migration (STU-15, VOC-17, DAT-12)."""

LEARNER = """
CREATE TABLE study_notes (
  note_id TEXT PRIMARY KEY CHECK (length(note_id) = 29),
  text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
  source TEXT NOT NULL CHECK (source IN ('typed','voice')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;

ALTER TABLE settings RENAME TO previous_settings;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('low_power','text_scale','locale',
                     'memory_saving','notifications','reduced_motion','read_aloud','easy_read',
                     'extra_time_percent','speech_rate','speech_voice')),
  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'extra_time_percent' OR value IN ('0','25','50'))),
  source           TEXT    NOT NULL CHECK (source IN ('default','student')),  -- setting_change actor_role
  set_by_event_id  TEXT,
  hlc              TEXT    NOT NULL,      -- hybrid logical clock stamp of the winning write
  updated_at       INTEGER NOT NULL
) STRICT;
INSERT INTO settings SELECT * FROM previous_settings;
DROP TABLE previous_settings;
"""
