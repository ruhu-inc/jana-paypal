"""One resumable local objective, separate from assessment evidence and conversational memory."""

LEARNER = """
CREATE TABLE learning_journey (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  body TEXT NOT NULL CHECK (json_valid(body)),
  source_id TEXT,
  source_revision INTEGER,
  FOREIGN KEY(source_id, source_revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
"""
