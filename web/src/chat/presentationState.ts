import { CustomerPresentation } from "../api";

interface PresentationMessageLike {
  id: string;
  role: "user" | "assistant";
  content?: string;
  presentation?: CustomerPresentation | null;
}

/**
 * Only the choice card at the end of the visible conversation is interactive.
 * This is presentation state, not a business-state decision; the server still
 * validates every subject_choice interaction.
 */
export function latestInteractiveChoiceMessageId(
  messages: readonly PresentationMessageLike[],
): string | null {
  let candidate: string | null = null;
  for (const message of messages) {
    if (message.role === "user") {
      candidate = null;
      continue;
    }
    if (message.presentation?.kind === "choice") {
      candidate = message.id;
      continue;
    }
    if (message.content || message.presentation) candidate = null;
  }
  return candidate;
}
