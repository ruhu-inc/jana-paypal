"""Immutable v1 SQLite schema for forward migrations (DAT-12)."""

LEARNER = """
CREATE TABLE meta (
  singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),

  profile_id       TEXT    NOT NULL,

  schema_version   INTEGER NOT NULL,

  created_at       INTEGER NOT NULL,

  created_app_ver  TEXT    NOT NULL,

  last_opened_at   INTEGER NOT NULL,

  last_app_ver     TEXT    NOT NULL
) STRICT;
CREATE TABLE profile (
  profile_id       TEXT    PRIMARY KEY,

  display_name     TEXT    NOT NULL CHECK (length(display_name) BETWEEN 1 AND 40),

  avatar           TEXT    NOT NULL,

  age_band         TEXT    NOT NULL CHECK (age_band IN ('under_13',
'13_15',
'16_17',
'18_plus')),

  level            TEXT    NOT NULL CHECK (level IN ('O',
'A')),

  subjects         TEXT    NOT NULL CHECK (json_valid(subjects)),

  exam_date        TEXT,

  reading_level    TEXT    CHECK (reading_level IN ('support',
'standard',
'advanced')),

  locale           TEXT    NOT NULL DEFAULT 'en' CHECK (locale GLOB '[a-z][a-z]*'),

  support_level    TEXT    NOT NULL DEFAULT 'standard' CHECK (support_level IN ('standard',
'extra')),

  goals            TEXT    CHECK (goals IS NULL OR json_valid(goals)),

  student_settings TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(student_settings)),

  created_at       INTEGER NOT NULL,

  updated_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE settings (
  key              TEXT    PRIMARY KEY CHECK (key IN ('solution_policy',
'low_power',
'text_scale',
'locale',

                     'memory_saving',
'notifications',
'reduced_motion',
'read_aloud',
'easy_read',
'extra_time_percent')),

  value            TEXT    NOT NULL CHECK (json_valid(value)
                     AND (key <> 'solution_policy' OR value IN ('"after_attempt"',
'"never"'))
                     AND (key <> 'extra_time_percent' OR value IN ('0',
'25',
'50'))),

  source           TEXT    NOT NULL CHECK (source IN ('default',
'student',
'adult',
'student_18_plus')),

  set_by_event_id  TEXT,

  hlc              TEXT    NOT NULL,

  updated_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE consents (
  consent_id       TEXT    PRIMARY KEY,

  scope            TEXT    NOT NULL CHECK (scope IN (
                     'cloud_tier',
'cloud_backup',
'multi_device',

                     'parent_remote_view',
'cloud_voice',
'study_materials',
'voice',
'research_use',

                     'model_training_research')),

  granted_by_role  TEXT    NOT NULL CHECK (granted_by_role IN ('guardian',
'student_18_plus')),

  granted_by_label TEXT    NOT NULL CHECK (length(granted_by_label) <= 60),

  basis_ref        TEXT,

  notice_version   TEXT    NOT NULL,

  dpia_ref         TEXT,

  method           TEXT    NOT NULL CHECK (method IN ('adult_pin_attestation',
'cloud_account_verified')),

  granted_at       INTEGER NOT NULL,

  expires_at       INTEGER,

  withdrawn_at     INTEGER,

  withdrawn_by_role TEXT   CHECK (withdrawn_by_role IN ('guardian',
'student_18_plus',
'student')),

  CHECK (scope = 'voice' OR dpia_ref IS NOT NULL)
) STRICT;
CREATE INDEX consents_scope_active ON consents(scope)
WHERE withdrawn_at IS NULL;
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
'adult',
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
CREATE INDEX events_type_seq ON events(type,
 seq);
CREATE INDEX events_session ON events(session_id,
 seq)
WHERE session_id IS NOT NULL;
CREATE INDEX events_sync ON events(sync_class,
 seq);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
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
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
SELECT RAISE(ABORT,
 'events are append-only; delete the profile instead'); END;
CREATE TABLE projection_state (
  name             TEXT    PRIMARY KEY,

  code_version     TEXT    NOT NULL,

  params_hash      TEXT    NOT NULL CHECK (length(params_hash) = 64),

  last_seq         INTEGER NOT NULL,

  last_hlc         TEXT,

  last_device_id   TEXT,

  last_origin_seq  INTEGER,

  rebuilt_at       INTEGER,

  checksum         TEXT,

  CHECK ((last_hlc IS NULL AND last_device_id IS NULL AND last_origin_seq IS NULL) OR
         (last_hlc IS NOT NULL AND last_device_id IS NOT NULL AND last_origin_seq IS NOT NULL))
) STRICT;
CREATE TABLE assessment_runs (
  assessment_id TEXT PRIMARY KEY,

  kind TEXT NOT NULL CHECK (kind IN ('diagnostic',
'mastery_check')),

  state TEXT NOT NULL CHECK (state IN ('in_progress',
'waiting',
'stopped',
'completed',
'failed')),

  body TEXT NOT NULL CHECK (json_valid(body)),

  updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE saved_questions (
  question_id TEXT PRIMARY KEY,

  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,

  content TEXT NOT NULL CHECK (json_valid(content)),

  status TEXT NOT NULL CHECK (status IN ('kept',
'sorted')),

  created_at INTEGER NOT NULL,

  expires_at INTEGER NOT NULL
) STRICT;
CREATE TABLE problem_reports (
  report_id TEXT PRIMARY KEY,

  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,

  content TEXT NOT NULL CHECK (json_valid(content)),

  created_at INTEGER NOT NULL,

  expires_at INTEGER NOT NULL
) STRICT;
CREATE TABLE lesson_positions (
  lesson_id        TEXT    PRIMARY KEY,

  block_index      INTEGER NOT NULL CHECK (block_index BETWEEN 0 AND 99),

  lesson_version   TEXT    NOT NULL,

  updated_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE archive_authorizations (
  authorization_id TEXT PRIMARY KEY,

  session_ids TEXT NOT NULL CHECK (json_valid(session_ids)),

  include_attachments INTEGER NOT NULL CHECK (include_attachments IN (0,
1)),

  profile_auth_epoch INTEGER NOT NULL,

  expires_at INTEGER NOT NULL,

  consumed_at INTEGER
) STRICT;
CREATE TABLE sessions (
  session_id       TEXT    PRIMARY KEY,

  mode             TEXT    NOT NULL CHECK (mode IN ('guided',
'solution',
'check_working',
'learn',
'practice',
'mock',
'revision',
'diagnostic',
'mastery_check',
'review')),

  subject          TEXT    CHECK (subject IN ('mathematics',
'physics',
'chemistry',
'biology',
'accounting')),

  topic_id         TEXT,

  kc_ids           TEXT    CHECK (kc_ids IS NULL OR json_valid(kc_ids)),

  item_ref         TEXT,

  model_tier       TEXT    CHECK (model_tier IN ('local_fast',
'local_strong',
'cloud',
'none')),

  hardware_class   TEXT    CHECK (hardware_class IN ('content_only',
'low',
'mid',
'high')),

  title            TEXT    CHECK (length(title) <= 80),

  summary          TEXT,

  started_at       INTEGER NOT NULL,

  ended_at         INTEGER,

  end_reason       TEXT    CHECK (end_reason IN ('completed',
'student_left',
'interrupted',
'idle_timeout',
'crash_recovered')),

  last_checkpoint_id TEXT,

  transcript_purged_at INTEGER
) STRICT;
CREATE INDEX sessions_started ON sessions(started_at);
CREATE TABLE turns (
  turn_id          TEXT    PRIMARY KEY,

  session_id       TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,

  idx              INTEGER NOT NULL,

  speaker          TEXT    NOT NULL CHECK (speaker IN ('student',
'tutor',
'system')),

  status           TEXT    NOT NULL CHECK (status IN ('open',
'complete',
'error',
'interrupted',
'truncated')),

  student_text     TEXT,

  input_kind       TEXT    CHECK (input_kind IN ('typed',
'photo',
'handwriting',
'voice',
'card_answer',
'action')),

  spoken_text      TEXT,

  heard_text       TEXT,

  heard_ms         INTEGER CHECK (heard_ms IS NULL OR heard_ms >= 0),

  trace_id         TEXT,

  usage            TEXT    CHECK (usage IS NULL OR json_valid(usage)),

  started_at       INTEGER NOT NULL,

  ended_at         INTEGER,

  UNIQUE (session_id,
 idx)
) STRICT;
CREATE TABLE parts (
  part_id          TEXT    PRIMARY KEY,

  turn_id          TEXT    NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,

  idx              INTEGER NOT NULL,

  kind             TEXT    NOT NULL CHECK (kind IN ('text',
'reasoning',
'tool',
'board',
'visual',
'card',
'notice')),

  status           TEXT    NOT NULL CHECK (status IN ('complete',
'error',
'interrupted',
'retracted')),

  card_type        TEXT    CHECK (card_type IN ('question',
'hint',
'plan',
'reflection',
'confirm')),

  content          TEXT    NOT NULL CHECK (json_valid(content)),

  created_at       INTEGER NOT NULL,

  updated_at       INTEGER NOT NULL,

  UNIQUE (turn_id,
 idx),

  CHECK ((kind = 'card') = (card_type IS NOT NULL))
) STRICT;
CREATE TABLE board_steps (
  step_id          TEXT    PRIMARY KEY,

  session_id       TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,

  board_id         TEXT    NOT NULL,

  part_id          TEXT    REFERENCES parts(part_id) ON DELETE SET NULL,

  idx              INTEGER NOT NULL,

  latex            TEXT    NOT NULL CHECK (length(latex) <= 4000),

  reason           TEXT    CHECK (length(reason) <= 300),

  owner            TEXT    NOT NULL CHECK (owner IN ('tutor',
'student')),

  status           TEXT    NOT NULL CHECK (status IN ('draft',
'final',
'awaiting',
'checking',
'correct',
'incorrect',
'equivalent_unsimplified',
'unchecked')),

  check_id         TEXT    REFERENCES checks(check_id),

  error_category   TEXT    CHECK (error_category IN ('sign',
'arithmetic',
'dropped_term',
'illegal_operation',
'units',
'copy',
'other')),

  superseded_at    INTEGER,

  created_at       INTEGER NOT NULL
) STRICT;
CREATE INDEX board_steps_session ON board_steps(session_id,
 idx);
CREATE TABLE checks (
  check_id         TEXT    PRIMARY KEY,

  session_id       TEXT    REFERENCES sessions(session_id) ON DELETE CASCADE,

  tool             TEXT    NOT NULL,

  tool_version     TEXT    NOT NULL,

  args_sha256      TEXT    NOT NULL,

  status           TEXT    NOT NULL CHECK (status IN ('ok',
'failed',
'error',
'timeout')),

  result           TEXT    NOT NULL CHECK (json_valid(result)),

  spill_ref        TEXT,

  created_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE attachments (
  attachment_id    TEXT    PRIMARY KEY CHECK (attachment_id GLOB 'img_[0-9A-Z]*' AND length(attachment_id) = 30),

  session_id       TEXT    REFERENCES sessions(session_id) ON DELETE CASCADE,

  kind             TEXT    NOT NULL CHECK (kind IN ('photo',
'diagram_crop',
'handwriting',
'ink_strokes',
'pdf_page')),

  file_path        TEXT    NOT NULL,

  sha256           TEXT    NOT NULL,

  bytes            INTEGER NOT NULL CHECK (bytes <= 2000000),

  width            INTEGER,

  height           INTEGER,

  transcription_part_id TEXT,

  created_at       INTEGER NOT NULL,

  delete_after     INTEGER NOT NULL
) STRICT;
CREATE INDEX attachments_delete_after ON attachments(delete_after);
CREATE TABLE checkpoints (
  checkpoint_id    TEXT    PRIMARY KEY,

  session_id       TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,

  turn_id          TEXT,

  last_event_seq   INTEGER NOT NULL,

  phase            TEXT    NOT NULL,

  state            TEXT    NOT NULL CHECK (json_valid(state)),

  state_version    INTEGER NOT NULL,

  created_at       INTEGER NOT NULL
) STRICT;
CREATE INDEX checkpoints_session ON checkpoints(session_id,
 created_at DESC);
CREATE TABLE kc_mastery (
  kc_id            TEXT    PRIMARY KEY,

  status           TEXT    NOT NULL CHECK (status IN ('not_started',
'learning',
'ready_to_check',
'mastered',
'needs_review',
'moved_on')),

  p_mastery        REAL    NOT NULL CHECK (p_mastery BETWEEN 0 AND 1),

  n_eff            REAL    NOT NULL DEFAULT 0 CHECK (n_eff >= 0),

  succ_w           REAL    NOT NULL DEFAULT 0 CHECK (succ_w >= 0),

  fail_w           REAL    NOT NULL DEFAULT 0 CHECK (fail_w >= 0),

  implied_w        REAL    CHECK (implied_w >= 0),

  attempts         INTEGER NOT NULL DEFAULT 0,

  correct          INTEGER NOT NULL DEFAULT 0,

  hints_used       INTEGER NOT NULL DEFAULT 0,

  bottom_outs      INTEGER NOT NULL DEFAULT 0,

  median_time_ms   INTEGER,

  fading_stage     INTEGER CHECK (fading_stage BETWEEN 0 AND 10),

  stuck_count      INTEGER NOT NULL DEFAULT 0,

  target_offset    REAL    CHECK (target_offset BETWEEN -0.05 AND 0.05),

  mastery_check_state TEXT NOT NULL DEFAULT 'none' CHECK (mastery_check_state IN ('none',
'scheduled',
'in_progress',
'passed',
'failed')),

  mastery_check    TEXT    CHECK (mastery_check IS NULL OR json_valid(mastery_check)),

  moved_on_at      INTEGER,

  last_seen_at     INTEGER,

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE TABLE review_cards (
  kc_id            TEXT    PRIMARY KEY,

  state            TEXT    NOT NULL CHECK (state IN ('learning',
'review',
'relearning')),

  stability        REAL    NOT NULL,

  difficulty       REAL    NOT NULL,

  due_at           INTEGER NOT NULL,

  reps             INTEGER NOT NULL DEFAULT 0,

  lapses           INTEGER NOT NULL DEFAULT 0,

  last_review_at   INTEGER,

  scheduler        TEXT,

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE INDEX review_cards_due ON review_cards(due_at);
CREATE TABLE learner_misconceptions (
  misconception_id TEXT    PRIMARY KEY,

  kc_id            TEXT    NOT NULL,

  status           TEXT    NOT NULL CHECK (status IN ('suspected',
'active',
'resolving',
'resolved',
'disputed')),

  evidence_count   INTEGER NOT NULL,

  fixes_tried      INTEGER NOT NULL DEFAULT 0,

  resolving_correct INTEGER,

  first_seen_at    INTEGER NOT NULL,

  last_seen_at     INTEGER NOT NULL,

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE INDEX learner_misconceptions_kc ON learner_misconceptions(kc_id,
 status);
CREATE TABLE attempts (
  attempt_id       TEXT    PRIMARY KEY,

  session_id       TEXT,

  item_ref         TEXT,

  step_id          TEXT,

  kc_ids           TEXT    NOT NULL CHECK (json_valid(kc_ids)),

  mode             TEXT    NOT NULL CHECK (mode IN ('guided',
'solution',
'check_working',
'learn',
'practice',
'mock',
'revision',
'diagnostic',
'mastery_check',
'review')),

  outcome          TEXT    NOT NULL CHECK (outcome IN ('correct',
'incorrect',
'partial',
'unverifiable')),

  hints_used       INTEGER NOT NULL,

  max_rung         INTEGER CHECK (max_rung BETWEEN 0 AND 5),

  time_ms          INTEGER NOT NULL,

  error_category   TEXT    CHECK (error_category IN ('value',
'units_missing',
'unit_error',
'power_of_ten',
'sig_figs',
'form',
'sign',
'arithmetic',
'dropped_term',
'illegal_operation',
'units',
'copy',
'wrong_side',
'wrong_account',
'unbalanced',
'missing_line',
'misconception',
'unreadable',
'other')),

  misconception_id TEXT,

  occurred_at      INTEGER NOT NULL
) STRICT;
CREATE INDEX attempts_time ON attempts(occurred_at);
CREATE TABLE engagement_daily (
  day              TEXT    PRIMARY KEY,

  minutes_active   INTEGER NOT NULL,

  sessions         INTEGER NOT NULL,

  attempts         INTEGER NOT NULL,

  solutions_viewed INTEGER NOT NULL,

  streak_length    INTEGER NOT NULL,

  freeze_used      INTEGER NOT NULL CHECK (freeze_used IN (0,
1)),

  frustration_signals INTEGER NOT NULL,

  gaming_flags     INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE TABLE engagement_state (
  id               INTEGER PRIMARY KEY CHECK (id = 1),

  streak_days      INTEGER NOT NULL DEFAULT 0 CHECK (streak_days >= 0),

  freezes_left     INTEGER NOT NULL DEFAULT 0 CHECK (freezes_left BETWEEN 0 AND 2),

  last_active_date TEXT,

  frustration      REAL    NOT NULL DEFAULT 0 CHECK (frustration BETWEEN 0 AND 1),

  confidence_bias  REAL    CHECK (confidence_bias BETWEEN -1 AND 1),

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE TABLE subject_ability (
  subject          TEXT    PRIMARY KEY,

  theta            REAL    NOT NULL,

  n                REAL    NOT NULL DEFAULT 0 CHECK (n >= 0),

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE TABLE preferences (
  key              TEXT    PRIMARY KEY CHECK (key IN ('start_style',
'representation',
'pace',
'unprompted_help',
'self_explanation_prompts')),

  value            TEXT    NOT NULL CHECK (json_valid(value)),

  confidence       REAL    NOT NULL CHECK (confidence BETWEEN 0 AND 1),

  last_event_seq   INTEGER NOT NULL
) STRICT;
CREATE TABLE notes (
  note_id          TEXT    PRIMARY KEY,

  subject          TEXT    CHECK (subject IN ('mathematics',
'physics',
'chemistry',
'biology',
'accounting')),

  kc_ids           TEXT    CHECK (kc_ids IS NULL OR json_valid(kc_ids)),

  text             TEXT    NOT NULL CHECK (length(text) <= 140),

  status           TEXT    NOT NULL CHECK (status IN ('active',
'pending_review',
'deleted')),

  source_event_ids TEXT    NOT NULL CHECK (json_valid(source_event_ids) AND json_array_length(source_event_ids) >= 1),

  source_session_id TEXT,

  created_at       INTEGER NOT NULL,

  updated_at       INTEGER NOT NULL,

  expires_at       INTEGER
) STRICT;
CREATE VIRTUAL TABLE notes_fts USING fts5(text,
 content='notes',
 content_rowid='rowid');
CREATE TRIGGER notes_fts_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid,
 text) VALUES (new.rowid,
 new.text); END;
CREATE TRIGGER notes_fts_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,
 rowid,
 text) VALUES ('delete',
 old.rowid,
 old.text); END;
CREATE TRIGGER notes_fts_au AFTER UPDATE OF text ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,
 rowid,
 text) VALUES ('delete',
 old.rowid,
 old.text);
  INSERT INTO notes_fts(rowid,
 text) VALUES (new.rowid,
 new.text); END;
CREATE TABLE study_plans (
  plan_id          TEXT    PRIMARY KEY,

  day              TEXT    NOT NULL,

  items            TEXT    NOT NULL CHECK (json_valid(items)),

  status           TEXT    NOT NULL CHECK (status IN ('proposed',
'started',
'changed',
'not_today',
'done')),

  created_at       INTEGER NOT NULL,

  updated_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE exams (
  exam_id          TEXT    PRIMARY KEY,

  paper_ref        TEXT    NOT NULL,

  status           TEXT    NOT NULL CHECK (status IN ('ready',
'in_progress',
'paused',
'interrupted',
'submitted',
'marked',
'reviewed',
'abandoned')),

  opens_at         INTEGER,

  closes_at        INTEGER,

  duration_ms      INTEGER NOT NULL,

  extra_time_percent INTEGER NOT NULL DEFAULT 0 CHECK (extra_time_percent IN (0,
25,
50)),

  started_at       INTEGER,

  elapsed_ms       INTEGER NOT NULL DEFAULT 0,

  pauses           TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(pauses)),

  interruptions    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(interruptions)),

  submitted_at     INTEGER,

  score_by_topic   TEXT    CHECK (score_by_topic IS NULL OR json_valid(score_by_topic))
) STRICT;
CREATE TABLE exam_answers (
  exam_id          TEXT    NOT NULL REFERENCES exams(exam_id) ON DELETE CASCADE,

  item_ref         TEXT    NOT NULL,

  answer           TEXT    CHECK (answer IS NULL OR json_valid(answer)),

  flagged          INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0,
1)),

  marks_awarded    REAL,

  marks_available  REAL,

  updated_at       INTEGER NOT NULL,

  PRIMARY KEY (exam_id,
 item_ref)
) STRICT;
CREATE TABLE alerts (
  alert_id         TEXT    PRIMARY KEY,

  kind             TEXT    NOT NULL CHECK (kind IN ('inactive',
'bottom_out_heavy',
'mastery_drop',

                     'mock_result',
'wellbeing_message_shown',
'pin_lockout')),

  severity         TEXT    NOT NULL CHECK (severity IN ('info',
'attention',
'urgent')),

  audience         TEXT    NOT NULL CHECK (audience IN ('parent')),

  payload          TEXT    NOT NULL CHECK (json_valid(payload)),

  created_at       INTEGER NOT NULL,

  seen_at          INTEGER,

  dismissed_at     INTEGER
) STRICT;
CREATE TABLE digests (
  digest_id        TEXT    PRIMARY KEY,

  week_start       TEXT    NOT NULL UNIQUE,

  content          TEXT    NOT NULL CHECK (json_valid(content)),

  generated_at     INTEGER NOT NULL,

  viewed_at        INTEGER
) STRICT;
CREATE TABLE sync_cursors (
  peer_id          TEXT    NOT NULL,

  direction        TEXT    NOT NULL CHECK (direction IN ('out',
'in')),

  last_seq         INTEGER NOT NULL,

  last_sync_at     INTEGER,

  PRIMARY KEY (peer_id,
 direction)
) STRICT;
CREATE TABLE audit_log (
  audit_id         TEXT    PRIMARY KEY,

  actor_role       TEXT    NOT NULL CHECK (actor_role IN ('student',
'adult',
'system')),

  action           TEXT    NOT NULL,

  target           TEXT,

  detail           TEXT    CHECK (detail IS NULL OR json_valid(detail)),

  at               INTEGER NOT NULL
) STRICT;
CREATE INDEX audit_log_at ON audit_log(at);
CREATE TABLE idempotency_responses (
  idempotency_key  TEXT    PRIMARY KEY,

  request_sha256   TEXT    NOT NULL,

  status           INTEGER NOT NULL,

  response         TEXT    NOT NULL CHECK (json_valid(response)),

  created_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE exports (
  export_id        TEXT    PRIMARY KEY,

  kind             TEXT    NOT NULL CHECK (kind IN ('profile_archive',
'parent_report',
'diagnostic_bundle',
'research_export')),

  destination      TEXT    NOT NULL CHECK (destination IN ('usb',
'file')),

  recipient        TEXT,

  event_seq_from   INTEGER,

  event_seq_to     INTEGER,

  sha256           TEXT    NOT NULL,

  created_at       INTEGER NOT NULL
) STRICT;

"""

DEVICE = """
CREATE TABLE device (
  singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),

  device_id        TEXT    NOT NULL,

  credential_public_key BLOB CHECK (credential_public_key IS NULL OR length(credential_public_key) = 32),

  credential_registered_at INTEGER,

  hardware_binding BLOB    NOT NULL CHECK (length(hardware_binding) = 32),

  hardware_class   TEXT    CHECK (hardware_class IN ('content_only',
'low',
'mid',
'high')),

  install_kind     TEXT    NOT NULL CHECK (install_kind IN ('family',
'school_lab',
'school_server')),

  created_at       INTEGER NOT NULL,

  schema_version   INTEGER NOT NULL
) STRICT;
CREATE TABLE profiles_index (
  profile_id       TEXT    PRIMARY KEY,

  kind             TEXT    NOT NULL CHECK (kind IN ('student')),

  display_name     TEXT    NOT NULL CHECK (length(display_name) BETWEEN 1 AND 40),

  avatar           TEXT    NOT NULL,

  db_file          TEXT    NOT NULL UNIQUE,

  keystore_ref     TEXT    NOT NULL UNIQUE,

  pin_hash         BLOB,

  profile_auth_epoch INTEGER NOT NULL DEFAULT 1,

  pin_failures     INTEGER NOT NULL DEFAULT 0,

  locked_until     INTEGER,

  pin_reset_at     INTEGER,

  pin_reset_acknowledged_at INTEGER,

  created_at       INTEGER NOT NULL,

  last_used_at     INTEGER,

  CHECK (pin_reset_acknowledged_at IS NULL OR (pin_reset_at IS NOT NULL AND pin_reset_acknowledged_at >= pin_reset_at))
) STRICT;
CREATE TABLE adult_pin (
  singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),

  pin_hash         BLOB    NOT NULL,

  recovery_hint    TEXT    CHECK (length(recovery_hint) <= 60),

  recovery_code_hash BLOB NOT NULL,

  recovery_generation INTEGER NOT NULL DEFAULT 1,

  failures         INTEGER NOT NULL DEFAULT 0,

  locked_until     INTEGER,

  updated_at       INTEGER NOT NULL
) STRICT;
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

  enabled_by_role  TEXT    CHECK (enabled_by_role IN ('adult')),

  enabled_at       INTEGER,

  disabled_at      INTEGER
) STRICT;
CREATE TABLE outbox (
  outbox_id        TEXT    PRIMARY KEY,

  idempotency_key  TEXT    NOT NULL UNIQUE,

  kind             TEXT    NOT NULL CHECK (kind IN ('tombstone',
'consent_withdrawal',
'telemetry',
'crash_report',
'backup_snapshot',
'multi_device_segment',
'parent_digest')),

  feature          TEXT,

  profile_id       TEXT,

  destination      TEXT    NOT NULL,

  payload          BLOB    NOT NULL,

  bytes            INTEGER NOT NULL CHECK (bytes >= 0),

  created_at       INTEGER NOT NULL,

  attempts         INTEGER NOT NULL DEFAULT 0,

  next_attempt_at  INTEGER,

  delivered_at     INTEGER,

  expires_at       INTEGER NOT NULL
) STRICT;
CREATE INDEX outbox_pending ON outbox(destination,
 next_attempt_at)
WHERE delivered_at IS NULL;
CREATE TABLE egress_ledger (
  ledger_id        TEXT    PRIMARY KEY,

  at               INTEGER NOT NULL,

  feature          TEXT    NOT NULL,

  destination      TEXT    NOT NULL,

  bytes            INTEGER NOT NULL,

  payload_class    TEXT    NOT NULL,

  profile_id       TEXT
) STRICT;
CREATE INDEX egress_ledger_at ON egress_ledger(at);
CREATE TABLE device_audit (
  audit_id         TEXT    PRIMARY KEY,

  actor_role       TEXT    NOT NULL CHECK (actor_role IN ('adult',
'system')),

  action           TEXT    NOT NULL CHECK (action IN ('profile.create',
'profile.import',
'profile.delete',
'profile.wipe',
'tombstone.expired',

                     'consent_withdrawal.expired',
'online_feature.enable',
'online_feature.disable',
'adult_pin.change',

                     'adult_pin.lockout',
'adult.initialized',
'adult.recovered',
'pin_reset.used',
'identity.regenerated',
'licence.activate')),

  profile_id       TEXT,

  detail           TEXT    CHECK (detail IS NULL OR json_valid(detail)),

  at               INTEGER NOT NULL
) STRICT;
CREATE INDEX device_audit_at ON device_audit(at);
CREATE TABLE idempotency_responses (
  idempotency_key  TEXT    PRIMARY KEY,

  request_sha256   TEXT    NOT NULL,

  status           INTEGER NOT NULL,

  response         TEXT    NOT NULL CHECK (json_valid(response)),

  created_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE device_consents (
  consent_id       TEXT    PRIMARY KEY,

  scope            TEXT    NOT NULL CHECK (scope IN ('telemetry',
'crash_reports')),

  granted_by_role  TEXT    NOT NULL CHECK (granted_by_role IN ('device_adult')),

  notice_version   TEXT    NOT NULL,

  dpia_ref         TEXT    NOT NULL,

  method           TEXT    NOT NULL CHECK (method IN ('adult_pin_attestation')),

  granted_at       INTEGER NOT NULL,

  withdrawn_at     INTEGER
) STRICT;
CREATE UNIQUE INDEX device_consents_one_active ON device_consents(scope)
WHERE withdrawn_at IS NULL;
CREATE TABLE app_settings (
  key              TEXT    PRIMARY KEY,

  value            TEXT    NOT NULL CHECK (json_valid(value)),

  updated_at       INTEGER NOT NULL
) STRICT;
CREATE TABLE migrations (
  db_kind          TEXT    NOT NULL CHECK (db_kind IN ('learner',
'device')),

  db_file          TEXT    NOT NULL,

  version          INTEGER NOT NULL,

  applied_at       INTEGER NOT NULL,

  app_version      TEXT    NOT NULL,

  backup_file      TEXT,

  PRIMARY KEY (db_file,
 version)
) STRICT;

"""

DATABASES = {"learner": LEARNER, "device": DEVICE}
