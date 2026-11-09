// The keyboard help ("?", TABLE LOC-12a): the shortcuts this build has, by where they work, each key drawn as a key.
// Ctrl reads Cmd on macOS. When the student has turned the single-character keys off, the ones they cover say so.
import type { ReactNode } from "react";
import { useT } from "../i18n.js";
import { Dialog } from "../primitives/index.js";

/** The shortcut rows, by group: the keys (each a list of key names joined by "+", alternatives separated) and the
 * catalogue key of what they do. `character` marks the single-character keys the student can turn off. */
export const SHORTCUTS: { group: "global" | "tutor" | "board" | "practice" | "mock"; keys: string[][]; action: string; character?: boolean }[] = [
  { group: "global", keys: [["Ctrl", "1"], ["Ctrl", "2"], ["Ctrl", "3"], ["Ctrl", "4"], ["Ctrl", "5"]], action: "keys.action.places" },
  { group: "global", keys: [["Ctrl", ","]], action: "keys.action.settings" },
  { group: "global", keys: [["/"]], action: "keys.action.ask", character: true },
  { group: "global", keys: [["?"]], action: "keys.action.help", character: true },
  { group: "global", keys: [["F6"], ["Shift", "F6"]], action: "keys.action.regions" },
  { group: "global", keys: [["Esc"]], action: "keys.action.escape" },
  { group: "tutor", keys: [["Enter"], ["Shift", "Enter"]], action: "keys.action.send" },
  { group: "tutor", keys: [["Alt", "T"], ["Alt", "H"], ["Alt", "S"]], action: "keys.action.hints" },
  { group: "tutor", keys: [["Ctrl", "Shift", "B"]], action: "keys.action.board" },
  { group: "tutor", keys: [["Ctrl", "M"]], action: "keys.action.dictate" },
  { group: "tutor", keys: [["Ctrl", "Shift", "V"]], action: "keys.action.voice_conversation" },
  { group: "board", keys: [["↑"], ["↓"]], action: "keys.action.lines" },
  { group: "board", keys: [["E"]], action: "keys.action.edit_line" },
  { group: "practice", keys: [["Alt", "H"]], action: "keys.action.help_me" },
  { group: "mock", keys: [["Alt", "←"], ["Alt", "→"]], action: "keys.action.mock_move" },
  { group: "mock", keys: [["Alt", "F"]], action: "keys.action.mock_flag" },
  { group: "mock", keys: [["Ctrl", "Enter"]], action: "keys.action.mock_submit" },
];

const GROUPS = ["global", "tutor", "board", "practice", "mock"] as const;

function isMac(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

function Keys({ keys }: { keys: string[][] }) {
  const t = useT();
  const mac = isMac();
  const name = (key: string) => (key === "Ctrl" ? t(mac ? "keys.cmd" : "keys.ctrl") : key === "Shift" ? t("keys.shift") : key === "Alt" ? t(mac ? "keys.option" : "keys.alt") : key === "Esc" ? t("keys.esc") : key === "Enter" ? t("keys.enter") : key);
  const out: ReactNode[] = [];
  keys.forEach((combo, i) => {
    if (i > 0) out.push(<span key={`or-${i}`} className="px-1 text-meta text-faint">{t("keys.or")}</span>);
    combo.forEach((key, j) => {
      if (j > 0) out.push(<span key={`plus-${i}-${j}`} aria-hidden="true" className="text-meta text-faint">+</span>);
      out.push(<kbd key={`${i}-${j}`} className="inline-flex min-h-7 min-w-7 items-center justify-center rounded-control border border-line-strong bg-panel px-2 font-mono text-meta text-ink">{name(key)}</kbd>);
    });
  });
  return <span className="flex flex-wrap items-center gap-1">{out}</span>;
}

export function KeyboardHelp({ open, onClose, characterKeys = true }: { open: boolean; onClose: () => void; characterKeys?: boolean }) {
  const t = useT();
  return (
    <Dialog open={open} heading={t("keys.title")} closeLabel={t("sheet.close")} onClose={onClose} wide>
      {!characterKeys && <p className="text-ui text-muted">{t("keys.character_off")}</p>}
      <div className="flex flex-col gap-6" data-keyboard-help="">
        {GROUPS.map((group) => (
          <section key={group} aria-labelledby={`keys-${group}`} className="flex flex-col gap-2">
            <h3 id={`keys-${group}`} className="text-label text-faint">{t(`keys.group.${group}`)}</h3>
            <dl className="flex flex-col divide-y divide-line">
              {SHORTCUTS.filter((s) => s.group === group).map((s) => (
                <div key={s.action} data-shortcut={s.action} data-off={s.character === true && !characterKeys ? "true" : undefined} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-2.5">
                  <dt className={`text-ui ${s.character === true && !characterKeys ? "text-faint" : "text-ink"}`}>{t(s.action)}</dt>
                  <dd><Keys keys={s.keys} /></dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Dialog>
  );
}
