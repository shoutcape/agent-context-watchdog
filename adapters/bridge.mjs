import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import { createConnection } from "node:net";

export function socketPath(env = process.env) {
  if (env.HERDR_ENV !== "1" || !env.HERDR_SOCKET_PATH || !env.HERDR_PANE_ID) return;
  const scope = createHash("sha256").update(env.HERDR_SOCKET_PATH).digest("hex").slice(0, 16);
  const state = join(env.ACW_HOME ?? join(env.XDG_STATE_HOME ?? join(homedir(), ".local/state"), "agent-context-watchdog"), scope);
  return join(env.XDG_RUNTIME_DIR ?? state, `acw-${scope}.sock`);
}

export function request(payload, env = process.env) {
  const path = socketPath(env);
  if (!path) return Promise.resolve({ ignored: "outside Herdr" });
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    let buffer = "";
    let settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      error ? reject(error) : resolve(result);
    };
    socket.setTimeout(5000, () => finish(new Error("Watchdog request timed out")));
    socket.on("error", (error) => finish(error));
    socket.on("end", () => { if (!settled) finish(new Error("Watchdog disconnected")); });
    socket.on("connect", () => socket.write(JSON.stringify({
      ...payload, pane: env.HERDR_PANE_ID, pids: [process.pid],
    }) + "\n"));
    socket.setEncoding("utf8");
    socket.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.length > 4 * 1024 * 1024) return finish(new Error("Watchdog response too large"));
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        const response = JSON.parse(buffer.slice(0, newline));
        finish(response.error ? new Error(response.error) : null, response.result);
      } catch (error) { finish(error); }
    });
  });
}
