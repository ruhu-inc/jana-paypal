"""Profile tokens (API-04, SEC-19, D86).

Tokens live only in the core's memory, so an app exit ends them all. A profile token names one profile and the
profile's auth epoch when it was issued: raising the epoch ends every token issued before it. It ends on close, on
switching to another profile, and after the inactivity close; each use restarts the inactivity clock. There is no
other kind of token: no adult token and no adult-only action (D86)."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Grant:
    subject: str  # the profile id
    epoch: int
    last_used: float


class TokenBook:
    def __init__(self, profile_idle_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.profile_idle_s = profile_idle_s
        self.clock = clock
        self.grants: dict[str, Grant] = {}
        self._lapsed: list[str] = []  # profiles whose token idled out, until the core has unloaded them (SEC-19)

    def issue(self, subject: str, epoch: int = 1) -> str:
        self.revoke()  # one open profile at a time: switching ends the previous one (SEC-19)
        token = secrets.token_urlsafe(32)  # base64url, so it is also a valid WebSocket subprotocol token
        self.grants[token] = Grant(subject, epoch, self.clock())
        return token

    def check(self, token: str | None, epoch_of: Callable[[str], int | None] | None = None) -> str | None:
        """The token's profile when it is live, else None (an expired token is removed)."""
        grant = self.grants.get(token or "")
        if grant is None:
            return None
        now = self.clock()
        current = epoch_of(grant.subject) if epoch_of else grant.epoch
        if now - grant.last_used > self.profile_idle_s or current != grant.epoch:
            del self.grants[str(token)]
            self._lapsed.append(grant.subject)
            return None
        grant.last_used = now
        return grant.subject

    def expires_in(self, token: str) -> int:
        return int(self.profile_idle_s)

    def revoke(self, *, subject: str | None = None) -> int:
        doomed = [t for t, g in self.grants.items() if subject is None or g.subject == subject]
        for token in doomed:
            del self.grants[token]
        return len(doomed)

    def lapsed_profiles(self) -> list[str]:
        """Ends every idled-out profile token and returns the profiles that lost their token since the last call, so
        the core can unload them at once rather than when the next profile opens (SEC-19, DAT-14)."""
        now = self.clock()
        for token, grant in list(self.grants.items()):
            if now - grant.last_used > self.profile_idle_s:
                del self.grants[token]
                self._lapsed.append(grant.subject)
        lapsed, self._lapsed = list(dict.fromkeys(self._lapsed)), []
        live = {g.subject for g in self.grants.values()}
        return [p for p in lapsed if p not in live]

    def active_profile(self) -> str | None:
        return next((g.subject for g in self.grants.values()), None)
