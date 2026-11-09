"""Durable companion turn provenance; text stays in the existing transcript tables and retention cascades."""

LEARNER = """
CREATE TABLE companion_turn_context (
  turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id) ON DELETE CASCADE,
  memory_revision TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('companion','memory_command')),
  egress_json TEXT NOT NULL CHECK (json_valid(egress_json))
) STRICT;
"""
