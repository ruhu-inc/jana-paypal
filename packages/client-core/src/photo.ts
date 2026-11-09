import type { CapturedQuestion, CapturePurpose, CaptureSource } from "./capture.js";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
export interface Corner { x: number; y: number }
export class PhotoCommands {
  constructor(private readonly commands: CommandClient, private readonly sessionId: string) {}
  create(file: Blob, source: CaptureSource, purpose: CapturePurpose): Promise<CapturedQuestion> {
    const key = clientId();
    const body = new FormData();
    body.append("meta", JSON.stringify({ client_capture_id: key, session_id: this.sessionId, source, purpose }));
    body.append("file", file, "capture");
    return this.commands.request<CapturedQuestion>("POST", "/v1/captures", body, key);
  }
  list(): Promise<{ captures: CapturedQuestion[] }> { return this.commands.get(`/v1/sessions/${this.sessionId}/captures`); }
  get(id: string): Promise<CapturedQuestion> { return this.commands.get(`/v1/captures/${id}`); }
  crop(id: string, corners: Corner[], rotation: number, previewOnly = false): Promise<CapturedQuestion> { return this.commands.patch(`/v1/captures/${id}/crop`, { corners, rotation, ...(previewOnly ? { preview_only: true } : {}) }, clientId()); }
  read(id: string, override: boolean, cloud = false): Promise<unknown> { return this.commands.post(`/v1/captures/${id}/read`, { override_quality: override, send_to_cloud: cloud }, clientId()); }
  /** `regions`: the read regions with the student's edited text, for a capture whose parts are named one per line (a
   * diagram to learn); the core keeps each region's place and takes only its text. */
  confirm(id: string, text: string, selected: string | null, regions?: CapturedQuestion["regions"]): Promise<CapturedQuestion> { return this.commands.post(`/v1/captures/${id}/confirm`, { text, selected_question: selected, ...(regions === undefined ? {} : { regions }) }, clientId()); }
  delete(id: string): Promise<void> { return this.commands.delete(`/v1/captures/${id}`, clientId()); }
  image(id: string, imageId: string): Promise<Blob> { return this.commands.request("GET", `/v1/captures/${id}/images/${imageId}`, undefined, null, {}, true); }
}
