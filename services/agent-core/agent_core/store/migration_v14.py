"""Bind a coaching job to its saved quiz question without changing quiz answers or source content."""

LEARNER = """
CREATE TABLE study_quiz_help_jobs (
  job_id TEXT PRIMARY KEY REFERENCES study_jobs(job_id) ON DELETE CASCADE,
  quiz_id TEXT NOT NULL REFERENCES study_quizzes(quiz_id) ON DELETE CASCADE,
  item_id TEXT NOT NULL
) STRICT;
"""
