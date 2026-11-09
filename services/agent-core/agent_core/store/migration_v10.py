"""M42 source workspace. Immutable revisions, independent reviews and deletion tombstones (DAT-22)."""

LEARNER = """
CREATE TABLE study_sources (
  source_id TEXT PRIMARY KEY CHECK (length(source_id) = 30),
  current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
  private INTEGER NOT NULL CHECK (private IN (0,1)),
  body TEXT NOT NULL CHECK (json_valid(body)), -- Source
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE study_revisions (
  source_id TEXT NOT NULL REFERENCES study_sources(source_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  digest TEXT NOT NULL CHECK (length(digest) = 64),
  blob_ref TEXT CHECK (blob_ref IS NULL OR (length(blob_ref) = 64 AND blob_ref NOT GLOB '*[^0-9a-f]*')),
  byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
  extracted_at INTEGER,
  PRIMARY KEY (source_id, revision)
) STRICT;
CREATE TABLE study_chunks (
  chunk_id TEXT PRIMARY KEY CHECK (length(chunk_id) = 30),
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  body TEXT NOT NULL CHECK (json_valid(body)), -- Chunk, includes text and locator
  UNIQUE(source_id, revision, ordinal),
  FOREIGN KEY(source_id, revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE study_jobs (
  job_id TEXT PRIMARY KEY CHECK (length(job_id) = 30),
  state TEXT NOT NULL CHECK (state IN ('queued','waiting_online','running','paused','completed','failed','cancelled')),
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  input_digest TEXT NOT NULL CHECK (length(input_digest) = 64),
  body TEXT NOT NULL CHECK (json_valid(body)), -- Job
  checkpoint TEXT NOT NULL CHECK (json_valid(checkpoint)), -- Checkpoint
  updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE study_job_sources (
  job_id TEXT NOT NULL REFERENCES study_jobs(job_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  PRIMARY KEY(job_id, source_id, revision),
  FOREIGN KEY(source_id, revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE study_aids (
  aid_id TEXT PRIMARY KEY CHECK (length(aid_id) = 30),
  body TEXT NOT NULL CHECK (json_valid(body)), -- Aid
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE study_aid_sources (
  aid_id TEXT NOT NULL REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  PRIMARY KEY(aid_id, source_id, revision),
  FOREIGN KEY(source_id, revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE study_decks (
  deck_id TEXT PRIMARY KEY CHECK (length(deck_id) = 30),
  aid_id TEXT NOT NULL UNIQUE REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  scheduler TEXT NOT NULL,
  FOREIGN KEY(source_id, revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
CREATE TABLE study_flashcards (
  card_id TEXT PRIMARY KEY CHECK (length(card_id) = 30),
  deck_id TEXT NOT NULL REFERENCES study_decks(deck_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  body TEXT NOT NULL CHECK (json_valid(body)), -- Flashcard
  schedule TEXT NOT NULL CHECK (json_valid(schedule)), -- CardSchedule
  due_at INTEGER NOT NULL
) STRICT;
CREATE INDEX study_flashcards_due ON study_flashcards(deck_id,due_at,card_id);
CREATE TABLE study_reviews (
  review_id TEXT PRIMARY KEY CHECK (length(review_id) = 30),
  card_id TEXT NOT NULL REFERENCES study_flashcards(card_id) ON DELETE CASCADE,
  body TEXT NOT NULL CHECK (json_valid(body)), -- ReviewResult
  reviewed_at INTEGER NOT NULL
) STRICT;
CREATE TABLE study_quizzes (
  quiz_id TEXT PRIMARY KEY CHECK (length(quiz_id) = 30),
  aid_id TEXT NOT NULL UNIQUE REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  body TEXT NOT NULL CHECK (json_valid(body)) -- QuizStored (keys remain private)
) STRICT;
CREATE TABLE study_attempts (
  attempt_id TEXT PRIMARY KEY CHECK (length(attempt_id) = 30),
  quiz_id TEXT NOT NULL REFERENCES study_quizzes(quiz_id) ON DELETE CASCADE,
  body TEXT NOT NULL CHECK (json_valid(body)), -- QuizResult
  submitted_at INTEGER NOT NULL
) STRICT;
CREATE TABLE study_tombstones (
  source_id TEXT PRIMARY KEY CHECK (length(source_id) = 30),
  deleted_at INTEGER NOT NULL
) STRICT;
CREATE TRIGGER study_source_erases_dependencies BEFORE DELETE ON study_sources BEGIN
  INSERT INTO study_tombstones(source_id,deleted_at) VALUES(OLD.source_id,unixepoch('now') * 1000)
    ON CONFLICT(source_id) DO NOTHING;
  DELETE FROM study_aids WHERE aid_id IN (SELECT aid_id FROM study_aid_sources WHERE source_id=OLD.source_id);
  DELETE FROM study_jobs WHERE job_id IN (SELECT job_id FROM study_job_sources WHERE source_id=OLD.source_id);
END;
CREATE TRIGGER study_source_no_resurrection BEFORE INSERT ON study_sources
WHEN EXISTS(SELECT 1 FROM study_tombstones WHERE source_id=NEW.source_id)
BEGIN SELECT RAISE(ABORT,'study source was deleted'); END;

"""
