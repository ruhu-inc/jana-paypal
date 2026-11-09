"""Immutable D85-D87 migration: PIN-free profiles, fixed answer policy and pausable consent (DAT-12)."""

LEARNER = """
DROP TRIGGER IF EXISTS events_append_only_update;
DROP TRIGGER IF EXISTS events_append_only_delete;
ALTER TABLE settings RENAME TO legacy_settings;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('low_power',
'text_scale',
'locale',

                     'memory_saving',
'notifications',
'reduced_motion',
'read_aloud',
'easy_read',
'extra_time_percent')),

  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'extra_time_percent' OR value IN ('0',
'25',
'50'))),

  source           TEXT    NOT NULL CHECK (source IN ('default',
'student')),

  set_by_event_id  TEXT,

  hlc              TEXT    NOT NULL,

  updated_at       INTEGER NOT NULL
) STRICT;
INSERT INTO settings (key,
value,
source,
set_by_event_id,
hlc,
updated_at)
SELECT key,
value,
source,
set_by_event_id,
hlc,
updated_at FROM legacy_settings
WHERE key <> 'solution_policy';
DROP TABLE legacy_settings;
ALTER TABLE consents RENAME TO legacy_consents;
CREATE TABLE consents (
  consent_id       TEXT    PRIMARY KEY,

  scope            TEXT    NOT NULL CHECK (scope IN (
                     'cloud_tier',
'cloud_backup',
'multi_device',

                     'parent_remote_view',
'cloud_voice',
'study_materials',
'research_use',

                     'model_training_research')),

  granted_by_role  TEXT    NOT NULL CHECK (granted_by_role IN ('guardian',
'student_18_plus')),

  granted_by_label TEXT    NOT NULL CHECK (length(granted_by_label) <= 60),

  basis_ref        TEXT,

  notice_version   TEXT    NOT NULL,

  dpia_ref         TEXT    NOT NULL,

  method           TEXT    NOT NULL CHECK (method IN ('checkout_guardian',
'in_app_guardian',
'in_app_notice')),

  granted_at       INTEGER NOT NULL,

  expires_at       INTEGER,

  paused_at        INTEGER,

  withdrawn_at     INTEGER,

  withdrawn_by_role TEXT   CHECK (withdrawn_by_role IN ('guardian',
'student_18_plus',
'student')),

  CHECK ((granted_by_role = 'guardian') = (method IN ('checkout_guardian',
'in_app_guardian'))),

  CHECK (method <> 'in_app_guardian' OR scope IN ('research_use',
'model_training_research')),

  CHECK (method <> 'checkout_guardian' OR scope NOT IN ('research_use',
'model_training_research'))
) STRICT;
INSERT INTO consents (consent_id,
scope,
granted_by_role,
granted_by_label,
basis_ref,
notice_version,
dpia_ref,
method,
granted_at,
expires_at,
withdrawn_at,
withdrawn_by_role)
SELECT consent_id,
scope,
granted_by_role,
granted_by_label,
basis_ref,
notice_version,
dpia_ref,
CASE
WHEN granted_by_role='student_18_plus'
THEN 'in_app_notice'
WHEN scope IN ('research_use',
'model_training_research')
THEN 'in_app_guardian'
ELSE 'checkout_guardian' END,
granted_at,
expires_at,
withdrawn_at,
withdrawn_by_role FROM legacy_consents
WHERE scope <> 'voice';
DROP TABLE legacy_consents;
ALTER TABLE events RENAME TO legacy_events;
CREATE TABLE events (
  seq              INTEGER PRIMARY KEY,

  event_id         TEXT    NOT NULL UNIQUE,

  profile_id       TEXT    NOT NULL,

  device_id        TEXT    NOT NULL,

  origin_seq       INTEGER NOT NULL,

  hlc              TEXT    NOT NULL,

  type             TEXT    NOT NULL,

  schema_version   TEXT    NOT NULL,

  source           TEXT    NOT NULL CHECK (source IN ('engine',
'checker',
'student',
'reflector',
'sync')),

  sync_class       TEXT    NOT NULL CHECK (sync_class IN ('consented',
'local')),

  occurred_at      INTEGER NOT NULL,

  recorded_at      INTEGER NOT NULL,

  session_id       TEXT,

  turn_id          TEXT,

  kc_ids           TEXT    CHECK (kc_ids IS NULL OR json_valid(kc_ids)),

  payload          TEXT    NOT NULL CHECK (json_valid(payload)),

  erased_at        INTEGER,

  UNIQUE (device_id,
 origin_seq)
) STRICT;
INSERT INTO events (seq,
event_id,
profile_id,
device_id,
origin_seq,
hlc,
type,
schema_version,
source,
sync_class,
occurred_at,
recorded_at,
session_id,
turn_id,
kc_ids,
payload,
erased_at)
SELECT seq,
event_id,
profile_id,
device_id,
origin_seq,
hlc,
type,
schema_version,
CASE
WHEN source='adult'
THEN 'student'
ELSE source END,
sync_class,
occurred_at,
recorded_at,
session_id,
turn_id,
kc_ids,
CASE
WHEN type='setting_change'
THEN json_set(json_remove(payload,
'$.pin_verified'),
'$.actor_role',
'student')
ELSE payload END,
erased_at FROM legacy_events
WHERE type <> 'pin_changed' AND NOT (type='setting_change' AND json_extract(payload,
'$.setting')='solution_policy');
DROP TABLE legacy_events;
CREATE TABLE support_shares (
  session_id       TEXT    PRIMARY KEY,

  confirmed_at     INTEGER NOT NULL,

  included_at      INTEGER
) STRICT;
ALTER TABLE alerts RENAME TO legacy_alerts;
CREATE TABLE alerts (
  alert_id         TEXT    PRIMARY KEY,

  kind             TEXT    NOT NULL CHECK (kind IN ('inactive',
'bottom_out_heavy',
'mastery_drop',

                     'mock_result',
'wellbeing_message_shown')),

  severity         TEXT    NOT NULL CHECK (severity IN ('info',
'attention',
'urgent')),

  audience         TEXT    NOT NULL CHECK (audience IN ('parent')),

  payload          TEXT    NOT NULL CHECK (json_valid(payload)),

  created_at       INTEGER NOT NULL,

  seen_at          INTEGER,

  dismissed_at     INTEGER
) STRICT;
INSERT INTO alerts (alert_id,
kind,
severity,
audience,
payload,
created_at,
seen_at,
dismissed_at)
SELECT alert_id,
kind,
severity,
audience,
payload,
created_at,
seen_at,
dismissed_at FROM legacy_alerts
WHERE kind <> 'pin_lockout';
DROP TABLE legacy_alerts;
ALTER TABLE audit_log RENAME TO legacy_audit_log;
CREATE TABLE audit_log (
  audit_id         TEXT    PRIMARY KEY,

  actor_role       TEXT    NOT NULL CHECK (actor_role IN ('student',
'system')),

  action           TEXT    NOT NULL,

  target           TEXT,

  detail           TEXT    CHECK (detail IS NULL OR json_valid(detail)),

  at               INTEGER NOT NULL
) STRICT;
INSERT INTO audit_log (audit_id,
actor_role,
action,
target,
detail,
at)
SELECT audit_id,
CASE
WHEN actor_role='adult'
THEN 'student'
ELSE actor_role END,
action,
target,
detail,
at FROM legacy_audit_log;
DROP TABLE legacy_audit_log;
CREATE INDEX IF NOT EXISTS consents_scope_active ON consents(scope)
WHERE withdrawn_at IS NULL;
CREATE INDEX IF NOT EXISTS events_type_seq ON events(type,
 seq);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id,
 seq)
WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_sync ON events(sync_class,
 seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
WHEN (
  OLD.erased_at IS NULL AND NEW.erased_at IS NOT NULL
  AND json_type(NEW.payload,
 '$') = 'object'
  AND json_type(NEW.payload,
 '$.erased') = 'true'
  AND json_type(NEW.payload,
 '$.erased_at') = 'text'
  AND json_extract(NEW.payload,
 '$.erased_at') GLOB '????-??-??T??:??:??*Z'
  AND julianday(json_extract(NEW.payload,
 '$.erased_at')) IS NOT NULL
  AND CAST(round((julianday(json_extract(NEW.payload,
 '$.erased_at')) - 2440587.5) * 86400000) AS INTEGER) = NEW.erased_at
  AND (SELECT count(*) FROM json_each(NEW.payload)) = 2
  AND NOT EXISTS (SELECT 1 FROM json_each(NEW.payload)
WHERE key NOT IN ('erased',
'erased_at'))
  AND NEW.seq = OLD.seq AND NEW.event_id = OLD.event_id AND NEW.profile_id = OLD.profile_id
  AND NEW.device_id = OLD.device_id AND NEW.origin_seq = OLD.origin_seq AND NEW.hlc = OLD.hlc
  AND NEW.type = OLD.type AND NEW.schema_version = OLD.schema_version AND NEW.source = OLD.source
  AND NEW.sync_class = OLD.sync_class AND NEW.occurred_at = OLD.occurred_at
  AND NEW.recorded_at = OLD.recorded_at
  AND NEW.session_id IS OLD.session_id AND NEW.turn_id IS OLD.turn_id AND NEW.kc_ids IS OLD.kc_ids) IS NOT 1
BEGIN
SELECT RAISE(ABORT,
 'events are append-only; only payload erasure is allowed'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
SELECT RAISE(ABORT,
 'events are append-only; delete the profile instead'); END;
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS board_steps_session ON board_steps(session_id,
 idx);
CREATE INDEX IF NOT EXISTS attachments_delete_after ON attachments(delete_after);
CREATE INDEX IF NOT EXISTS checkpoints_session ON checkpoints(session_id,
 created_at DESC);
CREATE INDEX IF NOT EXISTS review_cards_due ON review_cards(due_at);
CREATE INDEX IF NOT EXISTS learner_misconceptions_kc ON learner_misconceptions(kc_id,
 status);
CREATE INDEX IF NOT EXISTS attempts_time ON attempts(occurred_at);
CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid,
 text) VALUES (new.rowid,
 new.text); END;
CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,
 rowid,
 text) VALUES ('delete',
 old.rowid,
 old.text); END;
CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE OF text ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,
 rowid,
 text) VALUES ('delete',
 old.rowid,
 old.text);
  INSERT INTO notes_fts(rowid,
 text) VALUES (new.rowid,
 new.text); END;
CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log(at);
DELETE FROM archive_authorizations;
"""

DEVICE = """
ALTER TABLE profiles_index RENAME TO legacy_profiles_index;
CREATE TABLE profiles_index (
  profile_id       TEXT    PRIMARY KEY,

  kind             TEXT    NOT NULL CHECK (kind IN ('student')),

  display_name     TEXT    NOT NULL CHECK (length(display_name) BETWEEN 1 AND 40),

  avatar           TEXT    NOT NULL,

  db_file          TEXT    NOT NULL UNIQUE,

  keystore_ref     TEXT    NOT NULL UNIQUE,

  profile_auth_epoch INTEGER NOT NULL DEFAULT 1,

  created_at       INTEGER NOT NULL,

  last_used_at     INTEGER
) STRICT;
INSERT INTO profiles_index (profile_id,
kind,
display_name,
avatar,
db_file,
keystore_ref,
profile_auth_epoch,
created_at,
last_used_at)
SELECT profile_id,
kind,
display_name,
avatar,
db_file,
keystore_ref,
profile_auth_epoch,
created_at,
last_used_at FROM legacy_profiles_index;
DROP TABLE legacy_profiles_index;
ALTER TABLE online_features RENAME TO legacy_online_features;
CREATE TABLE online_features (
  feature          TEXT    PRIMARY KEY CHECK (feature IN (
                     'cloud_tier',
'downloads',
'update_checks',
'cloud_backup',
'multi_device',

                     'parent_remote_view',
'cloud_voice',
'study_materials',
'telemetry',
'crash_reports')),

  enabled          INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,
1)),

  enabled_by_role  TEXT    CHECK (enabled_by_role IN ('user',
'subscription')),

  enabled_at       INTEGER,

  disabled_at      INTEGER
) STRICT;
INSERT INTO online_features (feature,
enabled,
enabled_by_role,
enabled_at,
disabled_at)
SELECT feature,
enabled,
CASE
WHEN enabled_by_role='adult'
THEN 'user'
ELSE enabled_by_role END,
enabled_at,
disabled_at FROM legacy_online_features;
DROP TABLE legacy_online_features;
ALTER TABLE device_audit RENAME TO legacy_device_audit;
CREATE TABLE device_audit (
  audit_id         TEXT    PRIMARY KEY,

  actor_role       TEXT    NOT NULL CHECK (actor_role IN ('user',
'system')),

  action           TEXT    NOT NULL CHECK (action IN ('profile.create',
'profile.import',
'profile.delete',
'profile.wipe',
'tombstone.expired',

                     'consent_withdrawal.expired',
'online_feature.enable',
'online_feature.disable',
'lan_server.pin',
'lan_server.remove',

                     'identity.regenerated',
'licence.activate')),

  profile_id       TEXT,

  detail           TEXT    CHECK (detail IS NULL OR json_valid(detail)),

  at               INTEGER NOT NULL
) STRICT;
INSERT INTO device_audit (audit_id,
actor_role,
action,
profile_id,
detail,
at)
SELECT audit_id,
CASE
WHEN actor_role='adult'
THEN 'user'
ELSE actor_role END,
action,
profile_id,
detail,
at FROM legacy_device_audit
WHERE action NOT IN ('adult_pin.change',
'adult_pin.lockout',
'adult.initialized',
'adult.recovered',
'pin_reset.used');
DROP TABLE legacy_device_audit;
ALTER TABLE device_consents RENAME TO legacy_device_consents;
CREATE TABLE device_consents (
  consent_id       TEXT    PRIMARY KEY,

  scope            TEXT    NOT NULL CHECK (scope IN ('telemetry',
'crash_reports')),

  granted_by_role  TEXT    NOT NULL CHECK (granted_by_role IN ('device_user')),

  notice_version   TEXT    NOT NULL,

  dpia_ref         TEXT    NOT NULL,

  method           TEXT    NOT NULL CHECK (method IN ('in_app_notice')),

  granted_at       INTEGER NOT NULL,

  withdrawn_at     INTEGER
) STRICT;
INSERT INTO device_consents (consent_id,
scope,
granted_by_role,
notice_version,
dpia_ref,
method,
granted_at,
withdrawn_at)
SELECT consent_id,
scope,
'device_user',
notice_version,
dpia_ref,
'in_app_notice',
granted_at,
withdrawn_at FROM legacy_device_consents;
DROP TABLE legacy_device_consents;
DROP TABLE adult_pin;
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(destination,
 next_attempt_at)
WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS egress_ledger_at ON egress_ledger(at);
CREATE INDEX IF NOT EXISTS device_audit_at ON device_audit(at);
CREATE UNIQUE INDEX IF NOT EXISTS device_consents_one_active ON device_consents(scope)
WHERE withdrawn_at IS NULL;
"""

SQL = {"learner": LEARNER, "device": DEVICE}
