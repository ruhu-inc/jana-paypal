// Avatar discs (DSN-24): the profile's initial in `avatar-ink` on one of the eight `avatar-*` discs, assigned in order
// of creation and cycling after eight. Decorative: the name under the disc identifies the profile.

/** The disc fills, written out so the class scanner finds each one. */
export const AVATAR_DISCS = [
  "bg-avatar-1", "bg-avatar-2", "bg-avatar-3", "bg-avatar-4", "bg-avatar-5", "bg-avatar-6", "bg-avatar-7", "bg-avatar-8",
] as const;

/** The disc for a profile: the one its `avatar` names (`avatar-3`), otherwise by its place in creation order. */
export function discOf(avatar: string, order: number): number {
  const named = /^avatar-([1-8])$/.exec(avatar);
  return named === null ? (order % AVATAR_DISCS.length) + 1 : Number(named[1]);
}

/** `sm` is the 28 px disc of the sidebar's profile switch, `lg` 48 px, `xl` the 64 px disc of the picker and first run. */
export function Avatar({ name, disc, size = "lg" }: { name: string; disc: number; size?: "sm" | "lg" | "xl" }) {
  const initial = Array.from(name.trim())[0]?.toLocaleUpperCase() ?? "";
  const box = size === "sm" ? "size-7 text-label" : size === "xl" ? "size-16 text-title" : "size-12 text-title";
  return (
    <span aria-hidden="true" data-disc={disc} className={`inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-avatar-ink ${box} ${AVATAR_DISCS[(disc - 1) % AVATAR_DISCS.length]}`}>
      {initial}
    </span>
  );
}
