// Home's question box for a learner with no curriculum subjects: the words go to Your tutor in Study, which works from
// their own materials. No curriculum tutor session opens for them, so nothing here assumes a school subject.
import { useState } from "react";
import { useT } from "../i18n.js";
import { Button, FIELD_LOOK } from "../primitives/index.js";

export function CourseAsk({ onAsk }: { onAsk: (text: string) => void }) {
  const t = useT(), [text, setText] = useState("");
  return (
    <form className="flex flex-col items-start gap-3" data-course-ask="" onSubmit={(e) => { e.preventDefault(); if (text.trim()) onAsk(text.trim()); }}>
      <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("home.own_course.ask_label")}<textarea rows={2} maxLength={2000} className={FIELD_LOOK} value={text} onChange={(e) => setText(e.target.value)} /></label>
      <Button type="submit" tone="primary" disabled={!text.trim()}>{t("home.own_course.ask_send")}</Button>
    </form>
  );
}
