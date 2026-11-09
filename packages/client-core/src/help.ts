// Settings → Help's offline part (OPS-23, UXS-46, TABLE UXS-46a): the core's help articles, shipped with the app
// (`GET /v1/help`, `HelpReply`), with the known problems the compatibility matrix names (OPS-02), this computer's device
// id, app version and update channel, and the support contacts. Nothing leaves the computer: the core answers from its
// own files. The articles are catalogue keys (LOC-01, D93): their words are in the locale catalogues, so the screen shows
// them in the student's language and searches those words here (`searchHelp`). The reply is checked against the command
// contract at the boundary, and a reply that does not hold is refused. A core without the route (before M24) answers
// `not_found`: the help is then "unavailable".
import { errors, type api_commands } from "@jana/contracts-ts/client";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";

export type OfflineHelpReply = api_commands.HelpReply;
/** One article: its id and the catalogue keys of its title, body and steps. */
export type HelpArticle = OfflineHelpReply["articles"][number];
/** A known problem from the compatibility matrix: its title is a catalogue key; what to do is a help article. */
export type KnownIssue = OfflineHelpReply["known_issues"][number];

/** The help reply, checked: a field that does not hold refuses the whole reply (`invalid_reply`). */
export function readHelp(value: unknown): OfflineHelpReply {
  const problems = errors("api/commands#/$defs/HelpReply", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as OfflineHelpReply;
}

/** The catalogue prefix every article key carries; a key outside it is never shown (the core names only its own
 * articles' words, never another screen's). */
export const ARTICLE_KEY_PREFIX = "help.article.";

/** The articles whose words (title, body and steps, as `words` gives them in the student's locale) hold every word of
 * `query`, in the core's order; all of them for an empty query. Case is ignored. */
export function searchHelp(articles: readonly HelpArticle[], query: string, words: (key: string) => string): HelpArticle[] {
  const wanted = query.toLocaleLowerCase().split(/\s+/).filter((w) => w !== "");
  if (wanted.length === 0) return [...articles];
  return articles.filter((article) => {
    const text = [article.title_key, article.body_key, ...article.step_keys].map(words).join(" ").toLocaleLowerCase();
    return wanted.every((w) => text.includes(w));
  });
}

/** The article a known problem points to: its id is `help.<article id>` in the matrix. */
export function issueArticle(known: KnownIssue, articles: readonly HelpArticle[]): HelpArticle | null {
  const id = known.help_article_id.replace(/^help\./, "");
  return articles.find((a) => a.id === id) ?? null;
}

export class OfflineHelp {
  constructor(private readonly commands: CommandClient) {}

  /** Every article, with this computer's details; null when this computer's core does not serve help yet. */
  async read(): Promise<OfflineHelpReply | null> {
    try {
      return readHelp(await this.commands.get<unknown>("/v1/help"));
    } catch (error) {
      if (error instanceof CommandError && (error.code === "not_found" || error.status === 404)) return null;
      throw error;
    }
  }
}
