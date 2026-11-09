"""Reversible Study library organisation; sources and learning records remain unchanged."""

LEARNER = """
CREATE TABLE study_library_archive (
  source_id TEXT PRIMARY KEY REFERENCES study_sources(source_id) ON DELETE CASCADE
) STRICT;
"""
