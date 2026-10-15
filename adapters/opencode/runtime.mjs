import { request, socketPath } from "../bridge.mjs";

// V2.0.27's TUI client unwraps HTTP envelopes; the published client examples
// expose { data }. Keep this boundary explicit and test both shapes.
const data = (response) => response?.data ?? response;

// Report the provider's usage from the latest completed request as-is;
// never estimate content added since.
export function contextTokens(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.type === "compaction" && message.status === "completed") return null;
    if (message.type === "assistant" && message.tokens && message.time?.completed) {
      const usage = message.tokens;
      const counts = [usage.input, usage.output, usage.reasoning, usage.cache?.read, usage.cache?.write];
      if (counts.some((n) => !Number.isFinite(n) || n < 0)) return null;
      if (counts.every((n) => n === 0)) continue;
      return Math.ceil(counts.reduce((a, b) => a + b, 0));
    }
  }
  return null;
}

export async function sample(ctx, exchange = request) {
  const route = ctx.ui.router.current();
  if (route.type !== "session") return;
  const sessionID = route.sessionID;
  const [sessionResponse, contextResponse] = await Promise.all([
    ctx.client.session.get({ sessionID }),
    ctx.client.session.context({ sessionID }),
  ]);
  const session = data(sessionResponse);
  if (!session?.model) return;
  if (ctx.ui.router.current().sessionID !== sessionID) return;
  const serverIndex = process.argv.indexOf("--server");
  const serverEquals = process.argv.find((arg) => arg.startsWith("--server="));
  const response = await exchange({
    op: "observe", kind: "opencode", session: sessionID,
    model: `${session.model.providerID}/${session.model.id}`,
    tokens: contextTokens(data(contextResponse)), quality: "reported",
    cwd: session.location.directory, profile: session.agent,
    can_retire: !ctx.ui.tabs.enabled() || ctx.ui.tabs.list().length <= 1,
    server_args: serverIndex >= 0 ? ["--server", process.argv[serverIndex + 1]]
      : serverEquals ? [serverEquals] : [],
    standalone: process.argv.includes("--standalone"),
  });
  // Do not deliver to an obsolete route after an asynchronous fetch.
  if (ctx.ui.router.current().sessionID !== sessionID) return;
  if (response.prepare) {
    const preparation = response.prepare;
    try {
      let existing;
      try { existing = data(await ctx.client.session.get({ sessionID: preparation.session })); }
      catch { /* A deterministic ID lets a retried create recover safely. */ }
      if (!existing) {
        await ctx.client.session.create({
          id: preparation.session, title: `Context handoff ${preparation.id.slice(0, 12)}`,
          location: session.location, model: session.model, agent: session.agent,
          permissions: session.permissions,
        });
      }
      await exchange({ op: "prepared", id: preparation.id });
    } catch (error) {
      await exchange({ op: "prepared", id: preparation.id, error: String(error) });
    }
  }
  const notification = response.notification;
  if (notification) {
    // Use the connected client's auth/server context and durable idempotent admission.
    await ctx.client.session.synthetic({
      sessionID, id: notification.id, text: notification.text,
      description: "Context watchdog", delivery: "steer", resume: true,
    });
    await exchange({ op: "delivered", key: response.key, id: notification.id });
  }
}

export function start(ctx) {
  if (!socketPath()) return;
  let busy = false;
  let disposed = false;
  let warned = false;
  const tick = async () => {
    if (busy || disposed) return;
    busy = true;
    try {
      await sample(ctx);
      warned = false;
    } catch (error) {
      if (!warned) ctx.ui.toast.show({ message: `Context watchdog: ${String(error)}. Check acw status.`, variant: "warning" });
      warned = true;
    } finally { busy = false; }
  };
  const timer = setInterval(() => void tick(), 5000);
  void tick();
  return () => { disposed = true; clearInterval(timer); };
}
