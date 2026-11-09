"""The tutor's name and avatar join the canonical student-preference set (PRM-02, PRM-03, DAT-15, D99; DAT-12).

The settings table's CHECK names every key of the set, so it is rebuilt with `tutor_name` (a JSON string of 1 to 40
characters; the core checks the PRV-03 rules) and `tutor_avatar` (a JSON string naming an avatar of the app's set);
every stored row is kept as it was."""

LEARNER = """
ALTER TABLE settings RENAME TO previous_settings;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('low_power','text_scale','locale',
                     'memory_saving','notifications','reduced_motion','read_aloud','easy_read',
                     'extra_time_percent','speech_rate','speech_voice','theme','character_shortcuts',
                     'tutor_name','tutor_avatar')),
  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'extra_time_percent' OR value IN ('0','25','50'))
                     AND (key <> 'theme' OR value IN ('"system"','"light"','"dark"'))
                     AND (key <> 'character_shortcuts' OR value IN ('true','false'))
                     AND (key <> 'tutor_name' OR (json_type(value) = 'text'
                          AND length(json_extract(value, '$')) BETWEEN 1 AND 40))
                     AND (key <> 'tutor_avatar' OR (json_type(value) = 'text'
                          AND json_extract(value, '$') GLOB 'avatar.[a-z0-9_]*'))),
  source           TEXT    NOT NULL CHECK (source IN ('default','student')),  -- setting_change actor_role
  set_by_event_id  TEXT,
  hlc              TEXT    NOT NULL,      -- hybrid logical clock stamp of the winning write
  updated_at       INTEGER NOT NULL
) STRICT;
INSERT INTO settings SELECT * FROM previous_settings;
DROP TABLE previous_settings;
"""
