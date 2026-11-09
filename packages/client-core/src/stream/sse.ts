// An SSE parser over a byte stream (API-01). Clients read SSE with a streaming fetch reader, not EventSource, because
// EventSource cannot send the Authorization header or read a 409 body. Comment lines (`: hb`) are reported as
// heartbeats so the dead-stream timer can be reset.

export type SseItem = { kind: "frame"; id: string | null; event: string | null; data: string } | { kind: "heartbeat" };

export class SseParser {
  private buffer = "";
  private fields: { id: string | null; event: string | null; data: string[] } = { id: null, event: null, data: [] };
  private readonly decoder = new TextDecoder();

  /** Feed bytes; returns the complete items they finish. */
  push(chunk: Uint8Array): SseItem[] {
    this.buffer += this.decoder.decode(chunk, { stream: true });
    const items: SseItem[] = [];
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line === "") {
        if (this.fields.data.length > 0 || this.fields.event !== null) {
          items.push({ kind: "frame", id: this.fields.id, event: this.fields.event, data: this.fields.data.join("\n") });
        }
        this.fields = { id: null, event: null, data: [] };
        continue;
      }
      if (line.startsWith(":")) {
        items.push({ kind: "heartbeat" });
        continue;
      }
      const colon = line.indexOf(":");
      const name = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (name === "id") this.fields.id = value;
      else if (name === "event") this.fields.event = value;
      else if (name === "data") this.fields.data.push(value);
    }
    return items;
  }
}
