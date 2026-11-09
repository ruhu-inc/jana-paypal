"""M45.c imported past papers and device-local exam dates (ASM-27, LRN-44; D119)."""

LEARNER = """
CREATE TABLE past_papers (
  paper_id TEXT PRIMARY KEY CHECK (length(paper_id) = 30),
  subject TEXT NOT NULL CHECK (subject IN ('mathematics','physics','chemistry','biology','accounting')),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body TEXT NOT NULL CHECK (json_valid(body)),
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE exam_dates (
  exam_id TEXT PRIMARY KEY CHECK (length(exam_id) = 30),
  subject TEXT NOT NULL CHECK (subject IN ('mathematics','physics','chemistry','biology','accounting')),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  date TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'),
  time TEXT CHECK (time IS NULL OR time GLOB '[0-2][0-9]:[0-5][0-9]'),
  reminders INTEGER NOT NULL DEFAULT 1 CHECK (reminders IN (0,1)),
  dismissed TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(dismissed)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
"""
