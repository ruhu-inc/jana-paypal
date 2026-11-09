"""The theme preference joins the canonical student-preference set (DSN-05, DAT-15, D91; DAT-12).

The settings table's CHECK names every key of the set, so it is rebuilt with `theme` and the values it may hold;
every stored row is kept as it was."""

LEARNER = """
ALTER TABLE settings RENAME TO previous_settings;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('low_power','text_scale','locale',
                     'memory_saving','notifications','reduced_motion','read_aloud','easy_read',
                     'extra_time_percent','speech_rate','speech_voice','theme')),
  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'extra_time_percent' OR value IN ('0','25','50'))
                     AND (key <> 'theme' OR value IN ('"system"','"light"','"dark"'))),
  source           TEXT    NOT NULL CHECK (source IN ('default','student')),  -- setting_change actor_role
  set_by_event_id  TEXT,
  hlc              TEXT    NOT NULL,      -- hybrid logical clock stamp of the winning write
  updated_at       INTEGER NOT NULL
) STRICT;
INSERT INTO settings SELECT * FROM previous_settings;
DROP TABLE previous_settings;
"""
