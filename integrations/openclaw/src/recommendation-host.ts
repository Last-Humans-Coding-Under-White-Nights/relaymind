// RM37/RM39: this module supplies no production host authority or asset use API.
import type {
  ChoiceRequestV1,
  FeedbackRequestV1,
} from "./recommendation-contract.ts";
import type { UserActionClient } from "./recommendations.ts";
export type DirectActionInput =
  | { kind: "choose"; recommendationId: string; request: ChoiceRequestV1 }
  | { kind: "feedback"; recommendationId: string; request: FeedbackRequestV1 }
  | { kind: "listFeedback"; page: { afterId?: string; limit: number } }
  | {
      kind: "deleteFeedback";
      feedbackId: string;
      expectedFeedbackRevision: bigint;
    }
  | { kind: "resetFeedback"; expectedFeedbackRevision: bigint };
declare const authorizedSession: unique symbol;
export interface AuthorizedActionSession {
  readonly [authorizedSession]: true;
}
export interface HostBoundary {
  authorizeDirectInput(
    context: unknown,
    input: DirectActionInput,
  ): Promise<AuthorizedActionSession | null>;
}
export const UnavailableHostBoundary: HostBoundary = Object.freeze({
  async authorizeDirectInput() {
    return null;
  },
});
const clients = new WeakMap<AuthorizedActionSession, UserActionClient>();
/** Explicit deterministic-fixture injection only; this does not qualify a real host. */
export function createFixtureActionSession(
  client: UserActionClient,
): AuthorizedActionSession {
  const session = Object.freeze(Object.create(null)) as AuthorizedActionSession;
  clients.set(session, client);
  return session;
}
/** Opaque session lookup. Host/model assertions and structural lookalikes have no entry. */
export function actionClientForSession(
  session: AuthorizedActionSession,
): UserActionClient | undefined {
  return clients.get(session);
}
