"""Study packs from the research agent (SPEC §18): the approved brief, the research's progress, the gateway job it
follows, and the researched Study source and parts once made. The pages themselves become a Study source and live
with the source workspace."""

LEARNER = """
CREATE TABLE study_research (
  research_id TEXT PRIMARY KEY CHECK (research_id GLOB 'rsr_[0-9A-Z]*' AND length(research_id) = 30),
  gateway_id TEXT CHECK (gateway_id IS NULL OR (gateway_id GLOB 'rsj_[0-9A-Z]*' AND length(gateway_id) = 30)),
  body TEXT NOT NULL CHECK (json_valid(body)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;
"""
