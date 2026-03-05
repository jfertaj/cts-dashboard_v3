import { Page, Route } from "@playwright/test";

export interface ChatMockResponse {
  answer: string;
  last_table?: { columns: { key: string; label: string }[]; rows: Record<string, any>[] } | null;
  last_filters?: Record<string, any> | null;
}

/**
 * Installs a Playwright route that intercepts /api/ai/chat/stream and
 * returns a fake SSE stream with the given response.
 */
export async function mockChatStream(page: Page, response: ChatMockResponse) {
  await page.route("**/api/ai/chat/stream", async (route: Route) => {
    // Build SSE body: stream tokens then done event
    const words = response.answer.split(" ");
    const lines: string[] = [];
    for (const word of words) {
      lines.push(`data: ${JSON.stringify({ type: "token", text: word + " " })}\n\n`);
    }
    lines.push(
      `data: ${JSON.stringify({
        type: "done",
        answer: response.answer,
        // ChatView's handleArtifacts reads resp.table (not resp.last_table)
        table: response.last_table ?? null,
        last_filters: response.last_filters ?? null,
      })}\n\n`
    );
    const body = lines.join("");

    await route.fulfill({
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
      },
      body,
    });
  });
}

/**
 * Removes the chat stream mock so subsequent requests go through normally.
 */
export async function clearChatMock(page: Page) {
  await page.unroute("**/api/ai/chat/stream");
}

/**
 * Mock a chat response that includes a table.
 */
export function tableResponse(
  text: string,
  columns: { key: string; label: string }[],
  rows: Record<string, any>[]
): ChatMockResponse {
  return {
    answer: text,
    last_table: { columns, rows },
    last_filters: null,
  };
}
