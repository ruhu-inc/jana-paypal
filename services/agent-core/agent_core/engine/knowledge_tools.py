"""The knowledge toolset's host handlers and the Coach's grounding (TOL-21, TOL-29, MEM-21).

A session binds `content_search` and `curriculum_lookup` to the device's `KnowledgeIndex`, built from the installed,
signature-verified packs (CUR-08). Until one is bound neither tool is available, so neither is offered (TOL-06). Both
only read the index in memory: no learner data, no writes, no network. A refusal is `invalid_args` with a message that
names the rule.

`grounding` picks the passages of the session's topic for tier 5 (MEM-21): the topic of the KCs in play (the practice
item's, else the topic the session was opened on), ranked within the topic by the problem's words.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_core.config import Settings
from agent_core.prompts.builder import Passage as PromptPassage
from agent_core.retrieval.index import KnowledgeIndex
from agent_core.retrieval.knowledge import LookupRefused, content_search, curriculum_lookup
from agent_core.tools.spec import CallContext, InvalidArguments

KNOWLEDGE_TOOLS = frozenset({"content_search", "curriculum_lookup"})


@dataclass
class KnowledgeTools:
    index: KnowledgeIndex
    settings: Settings

    def bind(self, executors: dict[str, Any]) -> None:
        executors.update({"content_search": self.search, "curriculum_lookup": self.lookup})

    async def search(self, args: dict[str, Any], context: CallContext) -> dict[str, Any]:
        try:
            return content_search(self.index, args, self.settings)
        except LookupRefused as error:
            raise InvalidArguments(str(error)) from None

    async def lookup(self, args: dict[str, Any], context: CallContext) -> dict[str, Any]:
        try:
            return curriculum_lookup(self.index, args, self.settings)
        except LookupRefused as error:
            raise InvalidArguments(str(error)) from None

    def grounding(self, kc_ids: Sequence[str], query: str, max_chars: int) -> tuple[PromptPassage, ...]:
        """Tier-5 passages with full provenance and a contract-valid record id for the tutor's `cites` field."""
        topics = sorted({t for k in kc_ids if (t := self.index.topic_of_kc(k))})
        if not topics:
            return ()
        hits = self.index.grounding(topics, query, max_chars)
        # Pack text is untrusted material: markup in it is escaped before it is wrapped (PRM-19, PRV-16).
        return tuple(PromptPassage(f"{h.passage.pack_id}@{h.passage.pack_version}:{h.passage.id}",
                                   h.passage.text.replace("<", "&lt;").replace(">", "&gt;"), h.rank,
                                   cite_id=h.passage.record_id)
                     for h in hits)  # fmt: skip
