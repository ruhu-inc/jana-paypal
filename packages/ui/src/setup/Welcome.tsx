// The screens before a profile opens (UXS-09, UXS-10; direction A "Studio"): one card centred on `paper`, its words
// centred under the lockup where the brand belongs ("Who's learning?" and the profile picker, DSN-25), the title in
// display type.
import type { ReactNode } from "react";
import { BrandMark } from "../primitives/BrandMark.js";

export function WelcomeCard({ brand = false, labelledBy, children, ...data }: {
  brand?: boolean; labelledBy: string; children: ReactNode;
} & { [key: `data-${string}`]: string | undefined }) {
  return (
    <section {...data} aria-labelledby={labelledBy}
      className="flex w-full max-w-130 flex-col items-center gap-7 rounded-dialog border border-line bg-canvas px-8 pt-10 pb-8 text-center text-ink">
      {brand && <BrandMark kind="lockup" height={40} />}
      {children}
    </section>
  );
}
