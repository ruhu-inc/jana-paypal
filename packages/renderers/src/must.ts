// A value the drawing needs: present, or the renderer stops here and the viewer shows the words fallback (VSL-16).
export function must<T>(value: T | null | undefined): T {
  if (value === undefined || value === null) throw new RangeError("a value the drawing needs is missing");
  return value;
}
