"""W9 (SPEC §20.3, JM-D34): a video file's frames read as text, one row per frame; no image is stored."""

LEARNER = """
CREATE TABLE study_frames (
  frame_id TEXT PRIMARY KEY CHECK (frame_id GLOB 'frm_[0-9A-Z]*' AND length(frame_id) = 30),
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  at_ms INTEGER NOT NULL CHECK (at_ms >= 0 AND at_ms <= 7200000),
  reason TEXT NOT NULL CHECK (reason IN ('chapter','scene')),
  read_state TEXT NOT NULL CHECK (read_state IN ('read','unreadable','screened')),
  text TEXT NOT NULL,
  FOREIGN KEY(source_id, revision) REFERENCES study_revisions(source_id, revision) ON DELETE CASCADE
) STRICT;
"""
