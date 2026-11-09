"""Durable invalidation of model context after a learner edits or forgets memory.

History remains available to the learner. Prior dialogue is excluded from model recall conservatively, so an old
paraphrase cannot restore a removed fact. The marker commits with the memory edit and contains no learner text.

The marker itself is an `audit_log` row the store owns (`store.memory_context`, so a profile archive can carry it
without reaching this layer, ARC-18); this module is the memory layer's name for it: `write` advances it and the
engine reads the revision and cutoff from here."""

from __future__ import annotations

from agent_core.store.memory_context import ACTION as ACTION
from agent_core.store.memory_context import advance as advance
from agent_core.store.memory_context import cutoff as cutoff
from agent_core.store.memory_context import revision as revision
