// The hand-off's first-session interview (UXS-10 step 2, JRN-03 step 3; TOL-20): after "You're ready, {name}.", two
// short questions, one per card, rendered as the tutor's own question card. The questions are the core's scripted copy,
// the same while the tutor is still warming up (MDL-20), so the hand-off never waits for it. "Skip for now" is always
// there and asks nothing more; a question that cannot load or save never blocks learning: the student moves on.
import { useEffect, useId, useRef, useState } from "react";
import { answerInterview, readInterview, type CommandClient, type InterviewAnswer, type InterviewView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, ErrorState, Loader } from "../primitives/index.js";
import { CardView, type CardState } from "../transcript/parts.js";
import { WelcomeCard } from "./Welcome.js";

export function FirstRunInterviewView({ view, state, onAnswer, onSkip }: {
  view: InterviewView | "loading" | "failed";
  state: CardState;
  onAnswer: (answer: InterviewAnswer) => void;
  onSkip: () => void;
}) {
  const t = useT();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  const question = typeof view === "object" ? view.question : null;
  return (
    <WelcomeCard labelledBy={id} data-first-run-step="interview">
      <div className="flex flex-col gap-2">
        <h1 id={id} ref={heading} tabIndex={-1} className="text-display">{t("interview.title")}</h1>
        <p className="text-body text-muted">{t("interview.intro")}</p>
      </div>
      {view === "loading" && <Loader label={t("interview.loading")} />}
      {view === "failed" && <ErrorState message={t("interview.failed")} />}
      {typeof view === "object" && question !== null && (
        <div className="w-full text-start">
          <p className="text-meta text-muted">{t("interview.progress", { n: view.answered + 1, total: view.total })}</p>
          <CardView card={question.card} state={state} onAnswer={onAnswer} />
        </div>
      )}
      <Button tone="ghost" onClick={onSkip}>{t("interview.skip")}</Button>
    </WelcomeCard>
  );
}

export function FirstRunInterview({ commands, onDone }: { commands: CommandClient; onDone: () => void }) {
  const [view, setView] = useState<InterviewView | "loading" | "failed">("loading");
  const [state, setState] = useState<CardState>("open");
  const live = useRef(true);
  const finished = useRef(onDone);
  finished.current = onDone;
  useEffect(() => {
    live.current = true;
    // A core that cannot serve the interview never holds the student here: learning starts at once.
    readInterview(commands).then((found) => {
      if (!live.current) return;
      if (found.done) finished.current();
      else setView(found);
    }, () => live.current && finished.current());
    return () => {
      live.current = false;
    };
  }, [commands]);
  const answer = (given: InterviewAnswer) => {
    if (typeof view !== "object" || view.question === null || state === "answering") return;
    setState("answering");
    answerInterview(commands, view.question.question_id, given).then((next) => {
      if (!live.current) return;
      setState("open");
      if (next.done) onDone();
      else setView(next);
    }, () => live.current && setState("failed"));
  };
  // "Skip for now": the remaining questions are recorded as skipped, so they are not asked again; never waited on.
  const skip = () => {
    const remaining = typeof view === "object" ? view.total - view.answered : 0;
    const question = typeof view === "object" ? view.question : null;
    if (question !== null && remaining > 0) {
      void (async () => {
        let next: InterviewView | null = await answerInterview(commands, question.question_id, { skipped: true }).catch(() => null);
        while (next !== null && !next.done && next.question !== null) {
          next = await answerInterview(commands, next.question.question_id, { skipped: true }).catch(() => null);
        }
      })();
    }
    onDone();
  };
  return <FirstRunInterviewView view={view} state={state} onAnswer={answer} onSkip={skip} />;
}
