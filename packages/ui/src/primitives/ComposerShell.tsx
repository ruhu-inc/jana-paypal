// The floating composer's shell (UXS-15, DSN-08, D89): the one place the `composer` radius and the `dock` shadow appear.
// Whichever layout direction is chosen puts this same shell at the foot of the conversation, at most `composerMax`
// wide. It draws only the frame and the slots; the feature fills them: the text area, then a bar with add a photo and
// the maths keyboard on the start side and voice and the ink Send disc (Stop while the tutor answers) on the end side.
import type { ReactNode } from "react";

export function ComposerShell({ above, children, attach, keyboard, voice, send, below }: {
  /** Attachments and drafts that belong to the message (photo capture, dictation review), above the text. */
  above?: ReactNode;
  /** The text area. */
  children: ReactNode;
  attach?: ReactNode;
  keyboard?: ReactNode;
  voice?: ReactNode;
  send: ReactNode;
  /** Panels the tools open inside the composer (the dictation transcript, status lines). */
  below?: ReactNode;
}) {
  return (
    <div data-composer="" className="relative z-dock mx-auto flex w-full max-w-(--layout-composerMax) flex-col gap-1 rounded-composer bg-canvas px-3 pt-3 pb-2 text-ink shadow-dock focus-within:ring-1 focus-within:ring-focus">
      {above}
      <div className="min-w-0">{children}</div>
      <div data-composer-bar="" className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">{attach}{keyboard}</div>
        <div className="flex items-center gap-1">{voice}{send}</div>
      </div>
      {below}
    </div>
  );
}
