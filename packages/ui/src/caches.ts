// Render caches that hold the unlocked profile's text (rendered maths): the app clears them on every profile lock and
// switch with the store (SEC-19), through `ProfileController.onClear(clearRenderCaches)`.
import { katexCache } from "./maths/katex.js";

export function clearRenderCaches(): void {
  katexCache.clear();
}
