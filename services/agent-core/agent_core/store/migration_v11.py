"""M42 staged file replacement and content-free remote cleanup receipts (DAT-22)."""

LEARNER = """
CREATE TABLE study_pending_revisions (
  source_id TEXT PRIMARY KEY REFERENCES study_sources(source_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision >= 2),
  body TEXT NOT NULL CHECK (json_valid(body))
) STRICT;
CREATE TABLE study_cloud_cleanup (
  request_id TEXT NOT NULL CHECK (length(request_id) = 30),
  source_id TEXT NOT NULL CHECK (length(source_id) = 30),
  expires_at INTEGER NOT NULL,
  PRIMARY KEY(request_id, source_id)
) STRICT;
"""
