// The product's mark, lockup and app icon (DSN-25, D1, D89). They appear only in the fixed placements below: the launch
// screen, the top of the left rail, the profile picker and first run's "Who's learning?", Settings → About and the
// header of printed reports (the app icon, installer and USB
// kit are outside the interface). The interface, the board and the tutor's lines never show the brand; the tutor is
// "Tutor". The brand kit's artwork is used exactly as supplied (packages/ui/assets/brand, copied unchanged): never
// redrawn, recoloured, stretched or given effects.
import { useT } from "../i18n.js";
import { ErrorState, Loader } from "./index.js";

/** The gallery components the mark may appear in (`dsn25_brand_placements` checks every state against this). */
export const BRAND_PLACEMENTS = ["AppStart", "LeftRail", "ProfilePicker", "FirstRun", "SettingsAbout", "PrintedReport"] as const;

// The mark's two panels, the paths of the kit's jana-mark.svg (viewBox 0 0 70.26 100). The light and on-dark marks
// differ only in their fills, which are the `brand-hook` and `brand-stem` roles in each theme.
const MARK = { width: 70.26, height: 100 };
const HOOK = "M0 64.53 A2.37 2.37 0 0 1 1.18 62.48 L33.11 43.91 A0.3 0.3 0 0 1 33.55 44.17 L33.55 97.54 A1.54 1.54 0 0 1 31.34 98.92 L5.32 86.24 A9.47 9.47 0 0 1 0 77.73 Z";
const STEM = "M37.4 20.74 A1.87 1.87 0 0 1 38.31 19.13 L66.88 2.02 A2.23 2.23 0 0 1 70.26 3.93 L70.26 67.07 A9.57 9.57 0 0 1 65.7 75.22 L39.13 91.57 A1.13 1.13 0 0 1 37.4 90.61 Z";
// The lockup's wordmark is outlined artwork with its own ink, so the lockup and app icon are the kit's files.
const LOCKUP = { width: 293.4, height: 114 };
const FILES = {
  lockup: new URL("../../assets/brand/jana-lockup.svg", import.meta.url).href,
  lockupOnDark: new URL("../../assets/brand/jana-lockup-on-dark.svg", import.meta.url).href,
  appIcon: new URL("../../assets/brand/jana-app-icon.svg", import.meta.url).href,
  appIconSmall: new URL("../../assets/brand/jana-app-icon-small.svg", import.meta.url).href,
};
/** The kit's minimum sizes: the lockup 80 px wide, the mark 16 px; the small app icon serves 16 to 32 px. */
export const BRAND_MINIMUM = { lockupWidth: 80, mark: 16, smallIconMax: 32 } as const;

export type BrandKind = "mark" | "lockup" | "app-icon";

/** `surface="midnight"` is the launch screen's brand moment: the on-dark artwork whatever the theme. Elsewhere the
 * artwork follows the theme: the light files on light, the on-dark files on dark. */
export function BrandMark({ kind = "mark", height = 32, surface = "theme" }: { kind?: BrandKind; height?: number; surface?: "theme" | "midnight" }) {
  const t = useT();
  const label = t("brand.mark", { name: t("app.name") });
  const size = Math.max(height, BRAND_MINIMUM.mark);
  if (kind === "app-icon") {
    return <img data-brand="app-icon" src={size <= BRAND_MINIMUM.smallIconMax ? FILES.appIconSmall : FILES.appIcon} width={size} height={size} alt={label} />;
  }
  if (kind === "lockup") {
    const width = Math.max(Math.round((size * LOCKUP.width) / LOCKUP.height), BRAND_MINIMUM.lockupWidth);
    const tall = Math.round((width * LOCKUP.height) / LOCKUP.width);
    return (
      <span data-brand="lockup" role="img" aria-label={label} className="inline-flex shrink-0">
        {surface === "midnight" ? <img src={FILES.lockupOnDark} alt="" width={width} height={tall} /> : <>
          <img src={FILES.lockup} alt="" width={width} height={tall} className="block dark:hidden" />
          <img src={FILES.lockupOnDark} alt="" width={width} height={tall} className="hidden dark:block" />
        </>}
      </span>
    );
  }
  return (
    <svg data-brand="mark" data-theme={surface === "midnight" ? "dark" : undefined} role="img" aria-label={label}
      viewBox={`0 0 ${MARK.width} ${MARK.height}`} height={size} width={Math.round((size * MARK.width) / MARK.height)} className="shrink-0">
      <path d={HOOK} className="fill-brand-hook" />
      <path d={STEM} className="fill-brand-stem" />
    </svg>
  );
}

/** The launch screen (AppStart), the one brand moment on `midnight`: the on-dark lockup, then "Starting your tutor"
 * or the core's failure with "Try again". */
export function LaunchScreen({ failed, onRetry, product = "tutor" }: { failed: boolean; onRetry: () => void; product?: "tutor" | "studio" }) {
  const t = useT();
  return (
    <div className="flex min-h-full w-full flex-1 flex-col items-center justify-center gap-6 bg-midnight p-6 text-on-midnight" data-launch-screen={failed ? "failed" : "starting"}>
      <BrandMark kind="lockup" height={48} surface="midnight" />
      {failed ? <ErrorState message={product === "studio" ? t("studio.start_failed") : t("app.core_unavailable")} action={{ label: t("action.try_again"), onClick: onRetry }} /> : <Loader surface="on-midnight" label={product === "studio" ? t("studio.starting") : t("app.starting")} />}
    </div>
  );
}
