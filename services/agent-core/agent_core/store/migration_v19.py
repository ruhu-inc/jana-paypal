"""The Study companion's sessions (SPEC §19.3.2, W8 phase 1): `sessions.mode` admits `study` (the Study screen's
session of the day) and `study_companion` (the companion's daily episode, phase 2).

The CHECK only widens, so every stored row still satisfies it, and the table is changed in place as SQLite's
"other kinds of table schema changes" procedure describes: the stored definition is edited and the schema version
bumped (the runner does that for a migration marked `edits_schema`). A rebuild through a renamed copy is not used:
with foreign keys on, dropping the copy cascades into `turns`, `parts` and every other table that references
`sessions`, and the pragma that would prevent it cannot change inside the migration's transaction."""

LEARNER = """
PRAGMA writable_schema = ON;
UPDATE sqlite_schema
   SET sql = replace(sql, '''review''))', '''review'',''study'',''study_companion''))')
 WHERE type = 'table' AND name = 'sessions' AND instr(sql, '''study''') = 0;
PRAGMA writable_schema = OFF;
"""
