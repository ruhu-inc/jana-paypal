"""Durable source learning links, independent of expiring jobs and curriculum mastery."""

LEARNER = """
CREATE TABLE study_aid_parents (
  aid_id TEXT PRIMARY KEY REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  parent_aid_id TEXT NOT NULL REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  CHECK(aid_id != parent_aid_id)
) STRICT;
CREATE TABLE study_quiz_submissions (
  attempt_id TEXT PRIMARY KEY REFERENCES study_attempts(attempt_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('check','submit'))
) STRICT;
CREATE INDEX study_aid_parent ON study_aid_parents(parent_aid_id);
CREATE TABLE study_journeys (
  root_aid_id TEXT PRIMARY KEY REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  current_aid_id TEXT NOT NULL REFERENCES study_aids(aid_id) ON DELETE CASCADE,
  lesson_reviewed_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
) STRICT;
-- Preserve existing generated records byte-for-byte. Recover only explicit, revision-matching links.
INSERT INTO study_aid_parents
SELECT child.aid_id,parent.aid_id FROM study_aids child
JOIN study_jobs job ON job.job_id=json_extract(child.body,'$.job_id')
JOIN study_aids parent ON parent.aid_id=json_extract(job.checkpoint,'$.request.context_aid_id')
WHERE child.aid_id!=parent.aid_id
  AND json_extract(child.body,'$.sources')=json_extract(parent.body,'$.sources')
  AND (json_extract(parent.body,'$.kind')='chat'
       OR (json_extract(parent.body,'$.kind')='flashcards' AND json_extract(child.body,'$.kind')='quiz'));
-- Existing lesson -> cards chains are resumable without re-generating any content.
INSERT INTO study_journeys(root_aid_id,current_aid_id,updated_at)
SELECT parent.aid_id,parent.aid_id,max(child.created_at) FROM study_aid_parents link
JOIN study_aids parent ON parent.aid_id=link.parent_aid_id
JOIN study_aids child ON child.aid_id=link.aid_id
WHERE json_extract(parent.body,'$.kind')='chat' AND json_extract(child.body,'$.kind')='flashcards'
GROUP BY parent.aid_id;
"""
