// The first-session interview of the student hand-off (UXS-10 step 2, JRN-03 step 3): `GET /v1/interview` and
// `POST /v1/interview/{question_id}/answer` (API 1.10, D104). One question per card, in the events contract's
// `card_question` shape; each answer comes back as the interview after it, checked against `InterviewView`.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";

export type InterviewView = api_commands.InterviewView;
export type InterviewCard = api_commands.InterviewViewQuestionAnyOf1["card"];
/** An answer to one question: its option ids, the student's own words, or `skipped`. */
export type InterviewAnswer = Pick<api_commands.CardAnswer["answer"], "option_ids" | "text" | "skipped">;

function checked(value: unknown): InterviewView {
  const problems = errors("api/commands#/$defs/InterviewView", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as InterviewView;
}

/** Where the open profile's interview stands: the next question, or done. */
export async function readInterview(commands: CommandClient): Promise<InterviewView> {
  return checked(await commands.get<unknown>("/v1/interview"));
}

/** One answer (option ids, the student's own words, or `skipped`); retries of one answer carry the same key. */
export async function answerInterview(commands: CommandClient, questionId: string, answer: InterviewAnswer, key: string = clientId()): Promise<InterviewView> {
  const body: api_commands.CardAnswer = { client_answer_id: key, answer };
  return checked(await commands.post<unknown>(`/v1/interview/${encodeURIComponent(questionId)}/answer`, body, key));
}
