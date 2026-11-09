"""Forward-only addition to the gateway's idempotent feature ledger (there is no global schema counter)."""

SQL = """
CREATE TABLE IF NOT EXISTS companion_operations (
    day TEXT NOT NULL, holder TEXT NOT NULL, feature TEXT NOT NULL, operation TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK(attempts BETWEEN 1 AND 12),
    PRIMARY KEY(day, holder, feature, operation));
"""
