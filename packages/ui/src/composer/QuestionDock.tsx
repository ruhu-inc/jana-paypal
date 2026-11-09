import { useSession } from "../store.js";
import { CardView } from "../transcript/parts.js";
import { RequestDock, useDockRequest } from "./requests.js";
export function QuestionDock({ sessionId }: { sessionId: string }) {
  const request = useDockRequest(sessionId); const session = useSession();
  if (request?.card.card_type !== "question") return null;
  return <RequestDock requestId={request.request_id} kind="question"><CardView card={request.card} state={request.state} onAnswer={(answer) => { void session?.answerCard(request.request_id, answer).catch(() => undefined); }} /></RequestDock>;
}
