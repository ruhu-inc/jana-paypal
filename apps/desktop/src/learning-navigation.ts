import type { LearningJourneyView, MapTopic, SubjectMap } from "@jana/client-core";

export interface LearningObjective {
  node_id: string;
  kc_id: string;
  lesson_id?: string;
}

export function objectiveForKc(maps: Iterable<SubjectMap>, kcId: string): LearningObjective | null {
  for (const map of maps) for (const section of map.sections) for (const topic of section.topics) {
    if (topic.kcs.some((kc) => kc.kc_id === kcId)) return { node_id: topic.node_id, kc_id: kcId };
  }
  return null;
}

export function objectiveForTopic(topic: MapTopic, current: LearningJourneyView | null): LearningObjective | null {
  const kc = topic.kcs.find((k) => k.kc_id === current?.goal.kc_id)
    ?? topic.kcs.find((k) => !["mastered", "needs_review"].includes(k.state)) ?? topic.kcs[0];
  return kc ? { node_id: topic.node_id, kc_id: kc.kc_id } : null;
}

/** Keep the chosen lesson and, when it teaches it, the student's already active objective. */
export function objectiveForLesson(maps: Iterable<SubjectMap>, lessonId: string, current: LearningJourneyView | null): LearningObjective | null {
  for (const map of maps) for (const section of map.sections) for (const topic of section.topics) {
    const lesson = topic.lessons.find((l) => l.lesson_id === lessonId);
    if (!lesson) continue;
    const candidates = topic.kcs.filter((kc) => lesson.kc_ids.includes(kc.kc_id));
    const goal = objectiveForTopic({ ...topic, kcs: candidates }, current);
    if (goal) return { ...goal, lesson_id: lessonId };
  }
  return null;
}
