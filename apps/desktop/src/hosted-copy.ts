// The few sentences that would be false on the hosted demo, where a learner's data is on a server. Only a build with
// `VITE_JANA_HOSTED=1` uses these; the desktop app keeps its own catalogue.
export const HOSTED_COPY: Record<string, string> = {
  "first_run.privacy": "Your files are stored on the workshop server. When you choose online Study, Jana shares excerpts with the configured AI provider to answer your question.",
  "first_run.stays": "Your work is kept on this demo's server.",
  "connectivity.label.all_off": "On this server",
  "connectivity.line.all_off": "Everything stays on this demo's server.",
  "egress.first_use.keep_local": "Don't send it",
  "egress.first_use.keep_voice_local": "Don't send my voice",
};
