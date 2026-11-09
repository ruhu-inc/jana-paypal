"""Assessment command failures, translated by the API adapter (API-27)."""


class Refused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
