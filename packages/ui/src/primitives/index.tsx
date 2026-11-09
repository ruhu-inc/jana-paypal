// The component primitives (DSN-12): exactly one implementation each. Features compose these and never restyle or
// re-implement them. Native `title=` tooltips are not used anywhere.
import { Fragment, useEffect, useLayoutEffect, useId, useRef, useState, type ButtonHTMLAttributes, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type Ref } from "react";
import { Icon, type IconName } from "../icons.js";

type NativeButton = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title" | "className">;

/** The button looks of D89 (DSN-02): primary actions are ink (`action`), never blue; `emphasis` is the rare blue
 * forward step, at most one per screen and never New question, Send or a confirmation; `secondary` (the default,
 * also called `quiet`) and `ghost` are neutral; `danger` is the quiet system-only remove. Hover, pressed and selected
 * use the neutral `wash`, with a bolder label when pressed or selected. */
export type ButtonTone = "primary" | "emphasis" | "secondary" | "quiet" | "ghost" | "danger" | "reference";

const BUTTON_TONES: Record<ButtonTone, string> = {
  primary: "border-transparent bg-action text-action-ink hover:bg-action-hover",
  emphasis: "border-transparent bg-accent text-accent-ink hover:bg-accent-hover",
  secondary: "border-line-strong bg-canvas text-ink hover:bg-wash aria-pressed:bg-wash aria-selected:bg-wash",
  quiet: "border-line-strong bg-canvas text-ink hover:bg-wash aria-pressed:bg-wash aria-selected:bg-wash",
  ghost: "border-transparent bg-transparent text-ink hover:bg-wash aria-pressed:bg-wash aria-selected:bg-wash",
  danger: "border-transparent bg-transparent text-danger hover:bg-danger-soft",
  reference: "border-transparent bg-panel text-muted hover:bg-wash hover:text-ink",
};

/** Height: 44 px, the target floor (DSN-17); smaller drawings keep a 44 px hit area. */
export function Button({ tone = "secondary", size = "md", children, ...props }: NativeButton & { tone?: ButtonTone; size?: "md" | "sm" | "xs"; children: ReactNode; ref?: Ref<HTMLButtonElement> }) {
  const box = size === "xs" ? "min-h-7 min-w-9 rounded-pill px-2 text-meta after:absolute after:-inset-x-1 after:-inset-y-2 after:content-['']" : size === "sm" ? "min-h-9 px-3 after:absolute after:-inset-1 after:content-['']" : "min-h-11 px-4";
  return (
    <button type="button" {...props} className={`jana-press relative inline-flex items-center justify-center gap-2 ${size === "xs" ? "" : "rounded-control text-ui"} border font-medium aria-pressed:font-semibold aria-selected:font-semibold disabled:cursor-not-allowed disabled:border-transparent disabled:bg-panel disabled:text-faint ${box} ${BUTTON_TONES[tone]}`}>
      {children}
    </button>
  );
}

/** An icon-only button: its label is its accessible name and shows beside it on hover and on keyboard focus (UXS-01;
 * never a native `title=` tooltip). The shown label is decorative for assistive technology, which reads the name.
 * 40 px (`sm` 36 px) with the hit area extended to 44 px; `muted` at rest, `wash` and `ink` on hover and when pressed
 * (a toggle, `aria-pressed`), never a blue tint. `action` is the ink disc of the composer's Send and Stop (UXS-15). */
export function IconButton({ icon, label, tone = "quiet", size = "md", tip = "below", ...props }: NativeButton & {
  icon: IconName; label: string; tone?: "quiet" | "action"; size?: "md" | "sm"; tip?: "below" | "above";
}) {
  const look = tone === "action"
    ? "size-9 rounded-pill bg-action text-action-ink hover:bg-action-hover after:-inset-1 disabled:bg-panel disabled:text-faint"
    : `${size === "sm" ? "size-9 after:-inset-1" : "size-10 after:-inset-0.5"} rounded-control text-muted hover:bg-wash hover:text-ink aria-pressed:bg-wash aria-pressed:text-ink disabled:bg-transparent disabled:text-faint`;
  const place = tip === "above" ? "bottom-full mb-2" : "top-full mt-2";
  return (
    <span className="group relative inline-flex">
      <button type="button" aria-label={label} {...props} className={`jana-press relative inline-flex items-center justify-center after:absolute after:content-[''] disabled:cursor-not-allowed ${look}`}>
        <Icon name={icon} />
      </button>
      <span aria-hidden="true" data-icon-label="" className={`pointer-events-none absolute right-0 z-popover hidden whitespace-nowrap rounded-control bg-ink px-2.5 py-1.5 text-meta font-medium text-canvas shadow-overlay-1 group-hover:block group-has-[:focus-visible]:block ${place}`}>
        {label}
      </span>
    </span>
  );
}

/** A suggestion chip (UXS-14): one of up to three next steps above the composer, or a choice beside the Ask box (a
 * subject on Home). Neutral pill on `panel`; hover and pressed lay the `wash` over it (a flat overlay of one colour, not a
 * gradient), pressed with a bolder label. `hint` is the hint chip the student pulls hints with, in the hint learning
 * colour, its icon in the same ink (LRN-20, D112). */
export function SuggestionChip({ icon, tone = "neutral", children, ...props }: NativeButton & { icon?: IconName; tone?: "neutral" | "hint"; children: ReactNode }) {
  const look = tone === "hint" ? "bg-hint-soft text-hint" : "bg-panel text-ink";
  return (
    <button type="button" {...props} data-tone={tone === "hint" ? "hint" : undefined} className={`jana-press relative inline-flex min-h-11 items-center gap-2 rounded-chip px-4 text-ui font-medium whitespace-nowrap hover:bg-linear-to-r hover:from-wash hover:to-wash aria-pressed:bg-linear-to-r aria-pressed:from-wash aria-pressed:to-wash aria-pressed:font-semibold disabled:cursor-not-allowed disabled:text-faint ${look}`}>
      {icon !== undefined && <span className={`inline-flex ${tone === "hint" ? "" : "text-muted"}`}><Icon name={icon} size={16} /></span>}
      {children}
    </button>
  );
}

/** A status chip: a word with its icon on a soft fill (DSN-04, never colour alone). `verified` is a checked value,
 * `hint` the open hint rung, `look` a "look here" mark, `neutral` a quiet state such as "Your turn" or the subject. */
export type ChipTone = "neutral" | "verified" | "hint" | "look";

const CHIP_TONES: Record<ChipTone, string> = {
  neutral: "bg-panel text-muted",
  verified: "bg-verified-soft text-verified",
  hint: "bg-hint-soft text-hint",
  look: "bg-misconception-soft text-misconception",
};

/** A chip's frame for its tone and size, for a status that carries its own data attributes (a board line's check). */
export function chipLook(tone: ChipTone, size: "md" | "sm" = "md"): string {
  const box = size === "sm" ? "min-h-6 gap-1 px-2.5 text-label font-medium" : "min-h-7 gap-1.5 px-3 text-meta font-medium";
  return `inline-flex w-fit max-w-full items-center rounded-chip ${box} ${CHIP_TONES[tone]}`;
}

export function Chip({ tone = "neutral", icon, size = "md", children }: { tone?: ChipTone; icon?: IconName; size?: "md" | "sm"; children: ReactNode }) {
  return (
    <span data-chip={tone} className={chipLook(tone, size)}>
      {icon !== undefined && <Icon name={icon} size={16} />}
      <span className="flex min-w-0 items-center gap-1.5 truncate">{children}</span>
    </span>
  );
}

/** A subject's small dot (DSN-02): decorative, always beside the subject's name. */
const SUBJECT_DOTS: Record<string, string> = {
  physics: "bg-subject-physics", chemistry: "bg-subject-chemistry", mathematics: "bg-subject-mathematics",
  accounting: "bg-subject-accounting", biology: "bg-subject-biology",
};

export function SubjectDot({ subject }: { subject: string }) {
  return <span aria-hidden="true" data-subject-dot={subject} className={`inline-block size-2 shrink-0 rounded-pill ${SUBJECT_DOTS[subject] ?? "bg-line-strong"}`} />;
}

/** Two to four mutually exclusive options side by side (the theme setting, the board mode): a radio group with a
 * roving tab stop; arrow keys move the selection. Neutral: the chosen option is the `wash` with a bolder label. */
export function SegmentedControl<V extends string>({ label, options, value, onChange, disabled = false, wrap = false }: {
  label: string; options: { value: V; label: string }[]; value: V; onChange: (value: V) => void; disabled?: boolean; wrap?: boolean;
}) {
  const group = useRef<HTMLDivElement>(null);
  const move = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    let step = keys[event.key];
    if (step === undefined || disabled) return;
    event.preventDefault();
    // Left and right follow the reading direction (LOC-25); up and down do not.
    if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && group.current !== null && getComputedStyle(group.current).direction === "rtl") step = -step;
    const index = options.findIndex((o) => o.value === value);
    const next = options[(index + step + options.length) % options.length];
    if (next === undefined) return;
    onChange(next.value);
    group.current?.querySelector<HTMLElement>(`[data-value="${next.value}"]`)?.focus();
  };
  return (
    <div ref={group} role="radiogroup" aria-label={label} aria-disabled={disabled || undefined} onKeyDown={move} className={`inline-flex gap-0.5 rounded-control bg-panel p-1 ${wrap ? "max-w-full flex-wrap" : ""}`}>
      {options.map((option) => {
        const checked = option.value === value;
        return (
          <button key={option.value} type="button" role="radio" aria-checked={checked} data-value={option.value} tabIndex={checked ? 0 : -1} disabled={disabled}
            onClick={() => onChange(option.value)}
            className="jana-press min-h-9 rounded-control px-4 text-ui font-medium text-muted hover:text-ink aria-checked:bg-wash aria-checked:font-semibold aria-checked:text-ink disabled:cursor-not-allowed disabled:text-faint">
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** A notice in a learning colour (UXS-14, UXS-16): a checked value (`verified`), a hint (`hint`) or a "look here" note
 * (`look`) on its soft fill with its icon, a title and the words; the words stay `ink` on every fill, never colour
 * alone (DSN-04). `actions` sit under the words. */
export function Callout({ tone, icon, heading, children, actions, live = false, ...data }: {
  tone: Exclude<ChipTone, "neutral">; icon?: IconName; heading: ReactNode; children?: ReactNode; actions?: ReactNode; live?: boolean;
} & { [key: `data-${string}`]: string | undefined }) {
  const look = { verified: ["bg-verified-soft", "text-verified", "verified"], hint: ["bg-hint-soft", "text-hint", "hint"], look: ["bg-misconception-soft", "text-misconception", "look-here"] } as const;
  const [fill, mark, glyph] = look[tone];
  return (
    <div {...data} {...(live ? { role: "status", "aria-live": "polite" as const } : {})} data-callout={tone} className={`flex items-start gap-3 rounded-card px-4 py-3 text-ink ${fill}`}>
      <span className={`mt-0.5 inline-flex shrink-0 ${mark}`}><Icon name={icon ?? glyph} /></span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="text-ui font-semibold">{heading}</p>
        {children !== undefined && <div className="text-ui">{children}</div>}
        {actions !== undefined && <div className="mt-2 flex flex-wrap gap-2">{actions}</div>}
      </div>
    </div>
  );
}

/** A determinate progress bar (topics secure, a lesson's parts, a practice set): the label and the value in words above
 * a quiet track, or (`bare`) the track with the value beside it. The value in words is the bar's accessible value. */
export function ProgressBar({ label, value, max, valueText, bare = false }: { label: string; value: number; max: number; valueText: string; bare?: boolean }) {
  const share = max <= 0 ? 0 : Math.min(1, Math.max(0, value / max));
  const track = (
    <span role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-valuetext={valueText}
      className="relative block h-1.5 min-w-16 flex-1 overflow-hidden rounded-pill bg-line">
      <span data-progress-fill="" className="absolute inset-y-0 start-0 rounded-pill bg-action" style={{ width: `${share * 100}%` }} />
    </span>
  );
  if (bare) {
    return (
      <span className="flex min-w-0 flex-1 items-center gap-3">
        {track}
        <span aria-hidden="true" className="shrink-0 text-meta text-muted tabular-nums">{valueText}</span>
      </span>
    );
  }
  return (
    <span className="flex w-full flex-col gap-2">
      <span aria-hidden="true" className="flex items-baseline justify-between gap-3 text-meta">
        <span className="text-muted">{label}</span>
        <span className="shrink-0 font-medium whitespace-nowrap text-ink tabular-nums">{valueText}</span>
      </span>
      <span className="flex">{track}</span>
    </span>
  );
}

/** An on/off setting (DSN-12): its words and an optional line of help, then the switch, a native checkbox drawn as a
 * track with a thumb: `ink` (`action`) when on, never the browser's blue. */
export function Switch({ label, help, checked, onChange, disabled = false }: { label: string; help?: string; checked: boolean; onChange: (checked: boolean) => void; disabled?: boolean }) {
  const id = useId();
  return (
    <div className="flex min-h-11 items-center justify-between gap-6 py-2">
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <label htmlFor={id} className="inline-flex min-h-11 cursor-pointer items-center text-ui font-medium text-ink">{label}</label>
        {help !== undefined && <span id={`${id}-help`} className="text-meta text-muted">{help}</span>}
      </span>
      <span className="relative inline-flex h-6 w-10 shrink-0 items-center">
        <input id={id} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} aria-describedby={help !== undefined ? `${id}-help` : undefined}
          className="peer absolute inset-0 size-full cursor-pointer appearance-none rounded-pill bg-line-strong checked:bg-action disabled:cursor-not-allowed disabled:opacity-50" />
        <span aria-hidden="true" className="pointer-events-none absolute start-0.5 size-5 rounded-pill bg-canvas peer-checked:start-4.5 peer-disabled:opacity-50" />
      </span>
    </div>
  );
}

/** A modal dialog (DSN-12) for a page of reference over the screen (the keyboard help): labelled by its title, Tab
 * stays inside it, Escape or its close button closes it, and focus goes back to where it was. */
export function Dialog({ open, heading, closeLabel, onClose, children, wide = false }: { open: boolean; heading: string; closeLabel: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    box.current?.querySelector<HTMLElement>("h2")?.focus();
    return () => {
      if (before?.isConnected) before.focus();
    };
  }, [open]);
  if (!open) return null;
  const keep = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab" || box.current === null) return;
    const focusable = [...box.current.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])")];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <div role="presentation" className="fixed inset-0 z-dialog flex items-center justify-center bg-scrim p-4" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={id} onKeyDown={keep}
        className={`jana-reveal flex max-h-full w-full flex-col gap-4 overflow-y-auto rounded-dialog bg-canvas p-6 text-ink shadow-overlay-2 ${wide ? "max-w-160" : "max-w-110"}`}>
        <div className="flex items-start justify-between gap-3">
          <h2 id={id} tabIndex={-1} className="text-title">{heading}</h2>
          <span className="-me-2 -mt-1"><IconButton icon="dismiss" size="sm" label={closeLabel} onClick={onClose} /></span>
        </div>
        {children}
      </div>
    </div>
  );
}

/** A menu button (DSN-12): its trigger (a button with `aria-haspopup="menu"`) opens a list of choices below it, or
 * above it (`placement`, the student menu at the sidebar's foot); arrow keys, Home and End move between them, Escape
 * or a choice closes it and focus goes back to the trigger. A choice with `checked` is a radio choice (the mode menu).
 * A choice may carry an icon and a quiet hint at its end (a shortcut, a plan); choices in different `group`s are
 * separated by a rule; `header` is a quiet, non-interactive heading at the top of the list. */
export interface MenuItem {
  id: string; label: string; checked?: boolean; disabled?: boolean; onSelect: () => void;
  icon?: IconName; hint?: ReactNode; group?: string;
}
export function MenuButton({ trigger, label, items, align = "end", placement = "below", header, block = false, descriptive = false, constrain = false, onOpen }: {
  trigger: (props: { "aria-haspopup": "menu"; "aria-expanded": boolean; onClick: () => void; ref: (el: HTMLButtonElement | null) => void }) => ReactNode;
  label: string;
  items: MenuItem[];
  align?: "start" | "end";
  placement?: "below" | "above";
  header?: ReactNode;
  /** The trigger fills its row (the sidebar's student row) instead of sitting inline. */
  block?: boolean;
  /** Longer previews sit under their label in a bounded, scrollable menu. */
  descriptive?: boolean;
  /** Keep a compact toolbar menu inside the viewport, including at phone width. */
  constrain?: boolean;
  /** Called as the menu opens: what it shows can be read then, not before (the student menu's plan). */
  onOpen?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [menuSpace, setMenuSpace] = useState<{ above: boolean; height: number } | null>(null);
  const button = useRef<HTMLButtonElement | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const [shift, setShift] = useState(0);
  useLayoutEffect(() => {
    if (!open || !constrain) return;
    const fit = () => {
      const box = list.current?.getBoundingClientRect();
      if (!box) return;
      // Remove the previous translation before clamping the natural position.
      setShift(previous => {
        const start = box.left - previous;
        return Math.max(16, Math.min(start, window.innerWidth - box.width - 16)) - start;
      });
    };
    fit(); window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [open, constrain]);

  useEffect(() => {
    if (open) list.current?.querySelector<HTMLElement>("[role^='menuitem']:not([disabled])")?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!list.current?.contains(event.target as Node) && !button.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", away);
    return () => window.removeEventListener("pointerdown", away);
  }, [open]);
  const close = () => {
    setOpen(false);
    button.current?.focus();
  };
  const onKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const choices = [...(list.current?.querySelectorAll<HTMLElement>("[role^='menuitem']:not([disabled])") ?? [])];
    const at = choices.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape" || event.key === "Tab") {
      if (event.key === "Escape") event.preventDefault();
      close();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      choices[(at + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      (event.key === "Home" ? choices[0] : choices[choices.length - 1])?.focus();
    }
  };
  const place = (descriptive && menuSpace ? menuSpace.above : placement === "above") ? "bottom-full mb-2" : "top-full mt-2";
  const toggle = () => {
    if (!open) {
      if (descriptive && button.current) {
        const anchor = button.current.getBoundingClientRect();
        const aboveSpace = Math.max(0, anchor.top - 12), belowSpace = Math.max(0, window.innerHeight - anchor.bottom - 12);
        const above = placement === "above" ? aboveSpace >= Math.min(320, belowSpace) : belowSpace < 320 && aboveSpace > belowSpace;
        setMenuSpace({ above, height: Math.min(320, above ? aboveSpace : belowSpace) });
      }
      onOpen?.();
    }
    setOpen((v) => !v);
  };
  return (
    <span className={block ? "relative flex w-full" : "relative inline-flex"}>
      {trigger({ "aria-haspopup": "menu", "aria-expanded": open, onClick: toggle, ref: (el) => { button.current = el; } })}
      {open && (
        <div ref={list} role="menu" aria-label={label} onKeyDown={onKey}
          style={{ ...(descriptive && menuSpace ? { maxHeight: menuSpace.height } : {}), ...(constrain ? { translate: `${shift}px 0`, minWidth: "min(18rem, calc(100vw - 2rem))", maxWidth: "calc(100vw - 2rem)" } : {}) }}
          className={`jana-reveal absolute z-popover flex ${descriptive ? "max-h-80 w-80 max-w-[calc(100vw-3rem)] overflow-y-auto" : block || placement === "above" ? "min-w-72" : "min-w-56"} flex-col rounded-card border border-line bg-canvas p-1 shadow-overlay-1 ${place} ${align === "end" ? "end-0" : "start-0"}`}>
          {header !== undefined && <div className="flex flex-col gap-0.5 px-3 pt-2 pb-2">{header}</div>}
          {items.map((item, index) => (
            <Fragment key={item.id}>
              {(index > 0 ? items[index - 1]?.group !== item.group : header !== undefined) && <div role="separator" className="mx-2 my-1 border-t border-line" />}
              <button type="button" role={item.checked === undefined ? "menuitem" : "menuitemradio"} aria-checked={item.checked} disabled={item.disabled}
                onClick={() => {
                  item.onSelect();
                  close();
                }}
                className={`jana-press flex min-h-11 ${descriptive ? "shrink-0" : ""} items-center gap-3 rounded-control px-3 text-start text-ui text-ink hover:bg-wash focus-visible:bg-wash aria-checked:font-semibold disabled:cursor-not-allowed disabled:text-faint`}>
                {item.checked !== undefined && <span aria-hidden="true" className={`inline-flex ${item.checked ? "text-ink" : "invisible"}`}><Icon name="verified" size={16} /></span>}
                {item.icon !== undefined && <span aria-hidden="true" className="inline-flex text-muted"><Icon name={item.icon} size={16} /></span>}
                {descriptive ? <span className="flex min-w-0 flex-1 flex-col gap-1 py-2"><span>{item.label}</span>{item.hint !== undefined && <span className="text-meta text-muted">{item.hint}</span>}</span>
                  : <><span className="flex-1">{item.label}</span>{item.hint !== undefined && <span className="text-meta text-faint">{item.hint}</span>}</>}
              </button>
            </Fragment>
          ))}
        </div>
      )}
    </span>
  );
}

/** A labelled text field (DSN-12): the label above, the field on `canvas` with a `line-strong` edge, 44 px tall. */
export const FIELD_LOOK = "min-h-11 w-full rounded-control border border-line-strong bg-canvas px-3 text-body text-ink";

/** The one spinner (DSN-12); it keeps running under reduced motion (DSN-09). Decorative: its row says what runs. */
export function Spinner() {
  return (
    <span className="jana-spinner inline-flex">
      <Icon name="loading" size={16} />
    </span>
  );
}

/** `on-midnight` is for the launch screen, the one brand moment on `midnight` (DSN-25). */
export function Loader({ label, surface = "interface" }: { label: string; surface?: "interface" | "on-midnight" }) {
  return (
    <span role="status" className={`inline-flex items-center gap-2 text-ui ${surface === "on-midnight" ? "text-on-midnight-muted" : "text-muted"}`}>
      <Spinner />
      <span>{label}</span>
    </span>
  );
}

export function Skeleton({ lines = 2 }: { lines?: number }) {
  return (
    <span aria-hidden="true" className="flex flex-col gap-2">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="block h-4 rounded-chip bg-panel" style={{ width: `${90 - i * 20}%` }} />
      ))}
    </span>
  );
}

export function EmptyState({ message }: { message: string }) {
  return <p className="py-8 text-center text-ui text-muted">{message}</p>;
}

/** What happened, what still works, one action (UXS-05 rule 3). */
export function ErrorState({ message, action }: { message: string; action?: { label: string; onClick: () => void } }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-card border border-line-strong bg-canvas p-3 text-ui text-ink">
      <Icon name="system-error" />
      <span className="flex-1">{message}</span>
      {action !== undefined && <Button onClick={action.onClick}>{action.label}</Button>}
    </div>
  );
}

/** A soft, full-width system message: the fill and the icon carry the tone, the words stay `ink` on every fill; no
 * coloured edge. Its action is a quiet button. */
export function Notice({ tone = "info", message, action }: { tone?: "info" | "warn"; message: string; action?: { label: string; onClick: () => void } }) {
  const [fill, mark] = tone === "warn" ? ["bg-warn-soft", "text-warn"] : ["bg-info-soft", "text-info"];
  return (
    <div role="status" aria-live="polite" className={`flex items-center gap-3 rounded-card px-4 py-3 text-ui text-ink ${fill}`}>
      <span className={`inline-flex shrink-0 ${mark}`}><Icon name={tone === "warn" ? "warning" : "info"} /></span>
      <span className="flex-1">{message}</span>
      {action !== undefined && <Button tone="ghost" size="sm" onClick={action.onClick}>{action.label}</Button>}
    </div>
  );
}

export function Disclosure({ summary, label, open, onToggle, children }: { summary: ReactNode; label?: string; open: boolean; onToggle: () => void; children: ReactNode }) {
  const id = useId();
  return (
    <div>
      <button type="button" aria-label={label} aria-expanded={open} aria-controls={id} onClick={onToggle} className="inline-flex min-h-9 items-center gap-1 rounded-control text-meta text-muted hover:text-ink">
        <Icon name={open ? "collapse" : "expand"} size={16} />
        {summary}
      </button>
      <div id={id} className="jana-disclosure" data-open={open}>
        <div className="overflow-hidden" inert={!open} aria-hidden={!open}>{children}</div>
      </div>
    </div>
  );
}

/** A tooltip that works with keyboard and touch: it is the described-by text of its trigger, shown on focus. */
export function Tooltip({ text, children }: { text: string; children: (props: { "aria-describedby": string }) => ReactNode }) {
  const id = useId();
  return (
    <span className="group relative inline-flex">
      {children({ "aria-describedby": id })}
      <span id={id} role="tooltip" className="pointer-events-none absolute top-full left-0 z-popover mt-2 hidden rounded-control bg-ink px-2.5 py-1.5 text-meta font-medium text-canvas shadow-overlay-1 group-focus-within:block group-hover:block">
        {text}
      </span>
    </span>
  );
}

/** A popover; `label` makes it a labelled dialog, `above` opens it upwards from its anchor (a rail item at the bottom). */
export function Popover({ open, label, above = false, children }: { open: boolean; label?: string; above?: boolean; children: ReactNode }) {
  if (!open) return null;
  const place = above ? "bottom-full start-0 mb-2 w-80" : "";
  return (
    <div role={label === undefined ? undefined : "dialog"} aria-label={label} className={`absolute z-popover rounded-card border border-line bg-canvas p-3 shadow-overlay-1 ${place}`}>
      {children}
    </div>
  );
}

/** Names the consequence and puts the safe action first in focus order (DSN-12). Modal: Tab stays inside it, Escape is
 * the safe action, and focus goes back to where it was when it closes. A confirmation that removes something
 * (`destructive`) uses the quiet danger button, never a red fill; any other uses the ink primary. */
export function ConfirmDialog({ open, message, details = [], confirm, cancel, onConfirm, onCancel, destructive = false }: {
  open: boolean; message: string; details?: string[]; confirm: string; cancel: string; onConfirm: () => void; onCancel: () => void; destructive?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    box.current?.querySelector<HTMLElement>("button")?.focus(); // the safe action, first in the box
    return () => {
      if (before?.isConnected) before.focus();
    };
  }, [open]);
  if (!open) return null;
  const keep = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key !== "Tab" || box.current === null) return;
    const focusable = [...box.current.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])")];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <div role="presentation" className="fixed inset-0 z-dialog flex items-center justify-center bg-scrim p-4">
      <div ref={box} role="alertdialog" aria-modal="true" aria-label={message} onKeyDown={keep} className="w-full max-w-110 rounded-dialog bg-canvas p-6 text-ink shadow-overlay-2">
        <p className="mb-4 text-body">{message}</p>
        {details.map((line, i) => <p key={i} className="mb-4 text-ui text-muted">{line}</p>)}
        <div className="flex justify-end gap-2">
          <Button onClick={onCancel}>{cancel}</Button>
          <Button tone={destructive ? "danger" : "primary"} onClick={onConfirm}>{confirm}</Button>
        </div>
      </div>
    </div>
  );
}

export function Toast({ message }: { message: string }) {
  return (
    <div role="status" aria-live="polite" className="fixed right-4 bottom-4 z-toast rounded-card bg-ink px-4 py-2 text-ui text-canvas shadow-overlay-1">
      {message}
    </div>
  );
}

export function useToggle(initial: boolean): [boolean, () => void] {
  const [value, setValue] = useState(initial);
  return [value, () => setValue((v) => !v)];
}

