// LOC-14: one persistent polite region for settled tutor content; streaming projections never enter it.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { boardLineLatex, type PartRec, type StepRec } from "@jana/client-core";
import { shallowArray, useSelect } from "../store.js";
import { useT } from "../i18n.js";
import { useTutorName } from "../tutor/look.js";
import { MarkdownBlock } from "../markdown/Markdown.js";
import { Tex } from "../maths/Tex.js";

const signature = (record: PartRec | StepRec) => "part_id" in record ? record.text : JSON.stringify([record.index, record.owner, record.status, record.latex, record.check_id]);
const keyOf = (record: PartRec | StepRec) => "part_id" in record ? record.part_id : record.step_id;
function StepMath({ step }: { step: StepRec }) {
  const latex = step.owner === "student" ? boardLineLatex(step.latex) : step.latex;
  return latex === null ? <MarkdownBlock text={step.latex} /> : <Tex tex={latex} />;
}
export function TutorAnnouncements({ sessionId }: { sessionId: string }) {
  const t = useT(), tutor = useTutorName();
  const parts = useSelect((s) => {
    const turns = s.sessions[sessionId]?.turn_ids ?? [], last = turns[turns.length - 1];
    return (last ? s.turns[last]?.part_ids ?? [] : []).map((id) => s.parts[id]).filter((p): p is PartRec => p?.kind === "text" && p.status === "complete" && p.text.trim() !== "");
  }, shallowArray);
  const steps = useSelect((s) => {
    const boards = s.sessions[sessionId]?.board_ids ?? [], last = boards[boards.length - 1], board = last ? s.boards[last] : undefined;
    return (board?.step_ids ?? []).map((id) => board?.steps[id]).filter((line): line is StepRec => line !== undefined && !["draft", "checking"].includes(line.status));
  }, shallowArray);
  // A mount on an existing session is history. Subsequent settled additions/revisions are announcements.
  const seen = useRef(new Map([...parts, ...steps].map((record) => [keyOf(record), signature(record)])));
  const [announcement, setAnnouncement] = useState<ReactNode>(null);
  useEffect(() => {
    const records = [...parts, ...steps], changed = records.filter((record) => seen.current.get(keyOf(record)) !== signature(record));
    const next = new Map(records.map((record) => [keyOf(record), signature(record)]));
    const removed = [...seen.current.keys()].some((key) => !next.has(key));
    seen.current = next;
    if (changed.length) setAnnouncement(<>{changed.map((record) => "part_id" in record ?
      <div key={record.part_id}>{tutor}: <MarkdownBlock text={record.text} /></div> :
      <div key={record.step_id}>{t("board.step.number", { index: record.index + 1 })}, {record.owner === "student" ? t("message.you") : tutor}, <StepMath step={record} />, {t(`board.status.${record.status}`)}{record.status === "final" && record.check_id !== null ? ` · ${t("board.status.correct")}` : ""}</div>
    )}</>);
    else if (removed) setAnnouncement(null);
  }, [parts, steps, t, tutor]);
  return <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-tutor-announcements="">{announcement}</div>;
}
