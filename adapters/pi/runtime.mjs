import { request, socketPath } from "../bridge.mjs";

export async function sample(pi, ctx, exchange = request) {
  if (!ctx.model) return;
  const usage = ctx.getContextUsage();
  const session = ctx.sessionManager.getSessionId();
  const response = await exchange({
    op: "observe", kind: "pi", session,
    model: `${ctx.model.provider}/${ctx.model.id}`,
    tokens: usage?.tokens == null ? null : Math.ceil(usage.tokens),
    capacity: usage?.contextWindow, quality: "reported", cwd: ctx.cwd,
  });
  if (ctx.sessionManager.getSessionId() !== session) return;
  const notification = response.notification;
  if (notification) {
    // A persisted custom-message ID deduplicates delivery even after a Pi reload.
    const delivered = ctx.sessionManager.getEntries().some((entry) =>
      entry.type === "custom_message" && entry.customType === "agent-context-watchdog"
      && entry.details?.notificationID === notification.id);
    if (!delivered) {
      pi.sendMessage({
        customType: "agent-context-watchdog", content: notification.text, display: true,
        details: { notificationID: notification.id },
      }, { deliverAs: "steer", triggerTurn: true });
    }
    await exchange({ op: "delivered", key: response.key, id: notification.id });
  }
}

export default function extension(pi) {
  if (!socketPath()) return;
  let context;
  let timer;
  let busy = false;
  const tick = async () => {
    if (!context || busy) return;
    busy = true;
    try {
      await sample(pi, context);
      context?.ui.setStatus("context-watchdog", undefined);
    } catch {
      context?.ui.setStatus("context-watchdog", "watchdog unavailable: acw status");
    } finally { busy = false; }
  };
  pi.on("session_start", async (_event, ctx) => {
    context = ctx;
    clearInterval(timer);
    timer = setInterval(() => void tick(), 5000);
    await tick();
  });
  for (const event of ["turn_end", "agent_end", "session_compact", "model_select"]) {
    pi.on(event, async (_event, ctx) => { context = ctx; await tick(); });
  }
  pi.on("session_shutdown", () => { clearInterval(timer); context = undefined; });
}
