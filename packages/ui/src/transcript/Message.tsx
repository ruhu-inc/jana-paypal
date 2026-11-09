// The student's message bubble (UIA-05): keyed by its client id, so the optimistic bubble keeps its DOM node when the
// core accepts it; a failed send keeps the bubble with "Try again", never dropping it.
import { memo } from "react";
import { boardLineLatex, type MessageRec, type TurnRec } from "@jana/client-core";
import { useT } from "../i18n.js";
import { MarkdownBlock } from "../markdown/Markdown.js";
import { Button } from "../primitives/index.js";
import { useSelect, useSession } from "../store.js";

/** What the bubble shows: each line the student typed on its own line (working sent from the board is several), and a
 * line that is maths input ("c = 3.2x10^-4 mol dm^-3") typeset as the board shows it; a sentence stays as typed. */
function shownText(message: MessageRec): string {
  return message.content.map((block) => {
    if (!("text" in block)) return "latex" in block ? `\\(${block.latex}\\)` : "text_fallback" in block ? block.text_fallback : "";
    return block.text.split("\n").map((line) => {
      const latex = /[\d^_=²³⁻]/.test(line) ? boardLineLatex(line) : null;
      return latex === null ? line : `\\(${latex}\\)`;
    }).join("  \n");
  }).join("\n\n");
}

export const Bubble = memo(function Bubble({ messageKey }: { messageKey: string }) {
  const t = useT();
  const session = useSession();
  const message = useSelect((s) => s.messages[messageKey]);
  if (message === undefined) return null;
  return (
    <div data-message-key={messageKey} data-state={message.state} className="ms-auto flex w-fit max-w-[80%] flex-col gap-2 rounded-card bg-panel px-4 py-3 text-body [&_p]:my-0">
      <p className="sr-only">{t("message.you")}</p>
      <MarkdownBlock text={shownText(message)} />
      {message.state === "sending" && <p className="text-label text-faint">{t("composer.sending")}</p>}
      {message.state === "failed" && (
        <p className="flex items-center gap-2 text-label text-muted">
          {message.error_code !== undefined ? t(`error.${message.error_code}`) : t("composer.failed")}
          {session !== null && <Button onClick={() => session.retry(messageKey)}>{t("composer.retry")}</Button>}
        </p>
      )}
    </div>
  );
});

/** The student message that started a turn, found by its client id (or the message id for turns from a snapshot). */
export function StudentMessage({ turn }: { turn: TurnRec }) {
  const key = useSelect((s) => {
    if (turn.client_message_id !== null && s.messages[turn.client_message_id] !== undefined) return turn.client_message_id;
    const found = Object.values(s.messages).find((m) => m.message_id !== null && m.message_id === turn.user_message_id);
    return found?.key ?? null;
  });
  return key === null ? null : <Bubble messageKey={key} />;
}
