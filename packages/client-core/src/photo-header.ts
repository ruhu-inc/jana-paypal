// Inspect encoded dimensions before the webview allocates a preview (SEC-05).
export async function checkPhotoHeader(blob: Blob): Promise<void> {
  if (blob.size > 20 * 1024 * 1024) throw new Error("image_too_large");
  const view = new DataView(await blob.slice(0, 1024 * 1024).arrayBuffer());
  const ascii = (at: number, length: number) => Array.from({ length }, (_, i) => String.fromCharCode(view.getUint8(at + i))).join("");
  let width = 0; let height = 0;
  try {
    if (view.byteLength >= 24 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a) {
      width = view.getUint32(16); height = view.getUint32(20);
    } else if (view.byteLength >= 12 && view.getUint16(0) === 0xffd8) {
      let at = 2;
      while (at + 4 <= view.byteLength) {
        if (view.getUint8(at++) !== 0xff) break;
        while (view.getUint8(at) === 0xff) at += 1;
        const marker = view.getUint8(at++);
        if (marker === 0xd9 || marker === 0xda) break;
        const length = view.getUint16(at);
        if (length < 2) break;
        if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) {
          height = view.getUint16(at + 3); width = view.getUint16(at + 5); break;
        }
        at += length;
      }
    } else if (view.byteLength >= 30 && ascii(0, 4) === "RIFF" && ascii(8, 4) === "WEBP") {
      const format = ascii(12, 4);
      if (format === "VP8X") { width = 1 + (view.getUint32(23, true) >>> 8); height = 1 + (view.getUint32(26, true) >>> 8); }
      else if (format === "VP8 ") { width = view.getUint16(26, true) & 0x3fff; height = view.getUint16(28, true) & 0x3fff; }
      else if (format === "VP8L") { const bits = view.getUint32(21, true); width = (bits & 0x3fff) + 1; height = ((bits >>> 14) & 0x3fff) + 1; }
    }
  } catch { throw new Error("unsupported_format"); }
  if (width < 1 || height < 1) throw new Error("unsupported_format");
  if (width > 8000 || height > 8000) throw new Error("image_too_large");
}
