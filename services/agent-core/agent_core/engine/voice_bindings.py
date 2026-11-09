"""In-memory, epoch-scoped voice session capabilities (VOC-31).

The adapter uses only the binding line and last user message. Sidecar prompt text and assistant history are never
returned as tutor context. No binding, transcript, or token is persisted or included in diagnostic exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent_core.contracts.voice.engine_llm_adapter import Request
from agent_core.trace import new_ulid

_LINE = re.compile(r"jana-voice-binding: (vb_[0-9A-HJKMNP-TV-Z]{26})")


class BindingRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("voice_binding_rejected")


@dataclass(frozen=True, repr=False)
class VoiceBinding:
    binding_id: str
    session_id: str
    profile_id: str | None
    epoch: str

    @property
    def instructions(self) -> str:
        return f"jana-voice-binding: {self.binding_id}"


@dataclass(frozen=True, repr=False)
class BoundUtterance:
    binding: VoiceBinding
    text: str


@dataclass(repr=False)
class VoiceBindings:
    epoch: str
    _live: dict[str, VoiceBinding] = field(default_factory=dict)

    def mint(self, session_id: str, profile_id: str | None) -> VoiceBinding:
        self.revoke_session(session_id)
        binding = VoiceBinding(f"vb_{new_ulid()}", session_id, profile_id, self.epoch)
        self._live[binding.binding_id] = binding
        return binding

    def contains(self, binding: VoiceBinding) -> bool:
        return binding.epoch == self.epoch and self._live.get(binding.binding_id) is binding

    def revoke(self, binding: VoiceBinding) -> None:
        if self._live.get(binding.binding_id) is binding:
            del self._live[binding.binding_id]

    def revoke_session(self, session_id: str) -> None:
        self._live = {key: value for key, value in self._live.items() if value.session_id != session_id}

    def revoke_profile(self, profile_id: str | None) -> None:
        self._live = {key: value for key, value in self._live.items() if value.profile_id != profile_id}

    def clear(self) -> None:
        self._live.clear()

    def resolve(self, request: Request) -> BoundUtterance:
        lines = [
            line
            for message in request.messages
            if message.role == "system"
            for line in message.content.splitlines()
            if "jana-voice-binding:" in line
        ]
        match = _LINE.fullmatch(lines[0]) if len(lines) == 1 else None
        binding = self._live.get(match[1]) if match else None
        users = [message.content for message in request.messages if message.role == "user"]
        if binding is None or not self.contains(binding) or not users:
            raise BindingRejected()
        return BoundUtterance(binding, users[-1])
