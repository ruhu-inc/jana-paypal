"""Concept maps of a typed topic (Study, no document): from the pack, or from Muse Spark with no source, with the
learner's edits. A document's concept map is a study aid and lives with it."""

LEARNER = """
CREATE TABLE study_concept_maps (
  map_id TEXT PRIMARY KEY CHECK (map_id GLOB 'ctm_[0-9A-Z]*' AND length(map_id) = 30),
  body TEXT NOT NULL CHECK (json_valid(body)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
"""
