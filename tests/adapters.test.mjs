import { test, expect } from "bun:test";
import { contextTokens, sample as opencode } from "../adapters/opencode/runtime.mjs";
import { sample as pi } from "../adapters/pi/runtime.mjs";
import { socketPath } from "../adapters/bridge.mjs";

const assistant = (input) => ({ type: "assistant", time: { completed: 1 }, content: [],
  tokens: { input, output: 100, reasoning: 50, cache: { read: 90000, write: 10000 } } });

test("OpenCode uses latest per-request context with cache, not accumulated usage", () => {
  expect(contextTokens([assistant(900000), assistant(5000)])).toBe(105150);
  expect(contextTokens([assistant(900000), { type: "compaction", status: "completed" }])).toBeNull();
  expect(contextTokens([])).toBeNull();
});

test("OpenCode reports provider usage without estimating later messages", () => {
  const message = assistant(5000);
  message.content.push({ type: "tool", state: { status: "completed", content: [{ type: "text", text: "x".repeat(4000) }] } });
  expect(contextTokens([message, { type: "user", content: "y".repeat(4000) }])).toBe(105150);
});

function opencodeContext() {
  const calls = [];
  const ctx = {
    ui: { router: { current: () => ({ type: "session", sessionID: "ses_original" }) },
      tabs: { enabled: () => false, list: () => [] } },
    client: { session: {
      get: async ({ sessionID }) => {
        if (sessionID !== "ses_original") throw new Error("not found");
        return { data: { model: { providerID: "anthropic", id: "claude-haiku-5-5", variant: "high" },
          agent: "build", location: { directory: "/repo" }, permissions: [{ effect: "ask" }] } };
      },
      context: async () => ({ data: [assistant(10000)] }),
      synthetic: async (input) => calls.push(["synthetic", input]),
      create: async (input) => calls.push(["create", input]),
    } },
  };
  return { ctx, calls };
}

test("OpenCode delivers a durable synthetic reminder before marking delivered", async () => {
  const { ctx, calls } = opencodeContext();
  await opencode(ctx, async (packet) => {
    calls.push([packet.op, packet]);
    return packet.op === "observe" ? { key: "k", notification: { id: "msg_1", text: "handoff" } } : {};
  });
  expect(calls.map((x) => x[0])).toEqual(["observe", "synthetic", "delivered"]);
  expect(calls[1][1]).toMatchObject({ sessionID: "ses_original", id: "msg_1", delivery: "steer", resume: true });
});

test("OpenCode 2.0.27's TUI client returns unwrapped domain values", async () => {
  const { ctx } = opencodeContext();
  const get = ctx.client.session.get;
  ctx.client.session.get = async (input) => (await get(input)).data;
  ctx.client.session.context = async () => [assistant(10000)];
  const calls = [];
  await opencode(ctx, async (p) => { calls.push(p); return {}; });
  expect(calls[0]).toMatchObject({ op: "observe", model: "anthropic/claude-haiku-5-5", tokens: 110150 });
});

test("OpenCode prepares fresh context on its connected server with model variant and permissions", async () => {
  const { ctx, calls } = opencodeContext();
  await opencode(ctx, async (packet) => {
    calls.push([packet.op, packet]);
    return packet.op === "observe" ? { prepare: { id: "handoff", session: "ses_successor" } } : {};
  });
  expect(calls.map((x) => x[0])).toEqual(["observe", "create", "prepared"]);
  expect(calls[1][1]).toMatchObject({ id: "ses_successor", model: { variant: "high" }, permissions: [{ effect: "ask" }] });
  expect(calls[1][1].parentID).toBeUndefined();
});

test("OpenCode failed delivery remains unacknowledged", async () => {
  const { ctx } = opencodeContext();
  ctx.client.session.synthetic = async () => { throw new Error("offline"); };
  const packets = [];
  await expect(opencode(ctx, async (p) => { packets.push(p); return { key: "k", notification: { id: "n", text: "x" } }; })).rejects.toThrow("offline");
  expect(packets).toHaveLength(1);
});

test("Pi uses getContextUsage and custom steering, preserves unknown after compaction", async () => {
  const calls = [];
  const ctx = { cwd: "/repo", model: { provider: "p", id: "m" },
    getContextUsage: () => ({ tokens: null, contextWindow: 200000 }),
    sessionManager: { getSessionId: () => "s", getEntries: () => [] } };
  await pi({ sendMessage: (...args) => calls.push(args) }, ctx, async (p) => {
    calls.push([p]);
    return p.op === "observe" ? { key: "k", notification: { id: "n", text: "handoff" } } : {};
  });
  expect(calls[0][0].tokens).toBeNull();
  expect(calls[1][1]).toEqual({ deliverAs: "steer", triggerTurn: true });
  expect(calls[2][0].op).toBe("delivered");
});

test("Pi recognizes persisted reminder IDs on retry", async () => {
  let sends = 0;
  await pi({ sendMessage: () => sends++ }, {
    cwd: "/repo", model: { provider: "p", id: "m" }, getContextUsage: () => ({ tokens: 200000 }),
    sessionManager: { getSessionId: () => "s", getEntries: () => [
      { type: "custom_message", customType: "agent-context-watchdog", details: { notificationID: "n" } },
    ] },
  }, async (p) => p.op === "observe" ? { key: "k", notification: { id: "n", text: "x" } } : {});
  expect(sends).toBe(0);
});

test("Bridge scopes sockets to Herdr and ignores outside sessions", () => {
  expect(socketPath({})).toBeUndefined();
  const env = { HERDR_ENV: "1", HERDR_PANE_ID: "w1:p1", HERDR_SOCKET_PATH: "/a", XDG_RUNTIME_DIR: "/run/user/1" };
  expect(socketPath(env)).not.toBe(socketPath({ ...env, HERDR_SOCKET_PATH: "/b" }));
});
