# Agent Context Watchdog

A local background watchdog for **Herdr-managed OpenCode V2 TUI, Pi, and Claude Code** agents.

At the configured context threshold, it sends the agent a native reminder to assess its remaining work. The agent can finish normally or write a handoff and start a fresh successor through Herdr. The original pane can retire only after the successor explicitly acknowledges reading the handoff and being ready.

Defaults: **200,000 tokens globally; 100,000 for `claude-haiku-5-5`**. Limits are advisory and measure current context, never lifetime usage.

## Requirements

- Linux, Python 3.11+, Herdr with the current agent/pane CLI (developed against 0.8.2).
- OpenCode V2 TUI with the documented `@opencode/plugin/tui` API (developed against 2.0.27), Pi with extension context/steering APIs, or Claude Code with command hooks.
- No Python runtime dependencies. Bun is needed only for the JavaScript adapter tests.

## Install and run

Keep this checkout at a stable path. From the checkout, inside Herdr:

```sh
./bin/acw install
./bin/acw config
./bin/acw start
./bin/acw status
```

`install` links `~/.local/bin/acw` to this checkout and installs all three adapters. Use `--harness opencode`, `--harness pi`, or `--harness claude` to install just one. Existing JSON settings are merged and backed up beside the originals; unrelated hooks and settings are preserved. Add `~/.local/bin` to PATH if needed.

- **OpenCode:** adds the CLI-only plugin to `$XDG_CONFIG_HOME/opencode/cli.json`. CLI settings reload live; restart the TUI if the plugin has not loaded.
- **Pi:** adds `~/.pi/agent/extensions/context-watchdog.ts` (respects `PI_CODING_AGENT_DIR`). Reload extensions or restart Pi.
- **Claude Code:** adds SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, and Stop hooks to `~/.claude/settings.json` (respects `CLAUDE_CONFIG_DIR`). Restart Claude Code to ensure hooks are active. Subagents inside the same pane are excluded.

`start` launches a detached watchdog process, **not an agent**. One daemon covers all supported panes/workspaces in the current Herdr session. Run it once inside each Herdr session you want monitored. It does not install a boot-time service.

```sh
acw watch    # Foreground mode, suitable for a process supervisor
acw status  # JSON: context readings, missing/stale adapters, handoffs, errors
acw stop    # Stop only this Herdr session's watchdog
```

Stopping the watchdog does not stop agents. Adapters tolerate an unavailable daemon. Restarting the daemon preserves reminder and handoff state.

## Configuration

`acw config` creates/prints `$XDG_CONFIG_HOME/agent-context-watchdog/config.json` (default `~/.config/agent-context-watchdog/config.json`).

```json
{
  "global_limit": 200000,
  "model_limits": {
    "claude-haiku-5-5": 100000
  },
  "remind_every_tokens": 20000,
  "rearm_ratio": 0.9,
  "stale_after_seconds": 120,
  "discovery_interval_seconds": 5,
  "launch_args": {
    "opencode": [],
    "pi": [],
    "claude": []
  }
}
```

Resolution is **exact provider/model override → exact final model ID override → global fallback**. For example, `anthropic/claude-haiku-5-5` overrides a general `claude-haiku-5-5` entry. There are no fuzzy name matches or implicit version aliases. Add an explicit entry for providers using a different model ID.

Config reloads during discovery and observations. An invalid edit retains the last valid configuration and exposes `config_error` in status. Invalid configuration prevents initial startup.

- Notify when usage is **at least** the threshold.
- After delivery, remind after another `remind_every_tokens` tokens. Set this to `0` to disable repeat reminders.
- Falling below `limit × rearm_ratio` re-arms the initial notification. This avoids oscillating around a threshold.
- Model/threshold changes re-evaluate the policy.
- Unknown context does not become zero and does not deliver an old pending measurement.
- Delivery leases prevent simultaneous tool hooks from injecting duplicate reminders. A failed delivery becomes retryable after 15 seconds.

The watchdog does not override native compaction or silently clamp your limits. A model may compact before reaching 200k. Choose a lower threshold if you want time to hand off before that happens. Where capacity is reported, status flags a threshold at or above it.

`launch_args` supplies extra native CLI arguments for successors. Model selection is inherited automatically. Pi/Claude use their normal configuration and permissions; per-session flags or permission choices are not inferred. Configure desired flags explicitly. Do not include resume/continue flags: successors must start fresh.

## Handoff workflow

The reminder includes commands with the exact original pane and harness session ID. An agent choosing to hand off should:

1. Write a Markdown document with the user goal, constraints, cwd/worktree, completed work, changes, checks, decisions, blockers, and concrete next steps.
2. Stop modifying shared files.
3. Run:

   ```sh
   acw --pane ORIGINAL_PANE handoff start --session SESSION_ID \
     --document /absolute/path/handoff.md --quiesced
   ```

4. Inspect the returned handoff ID:

   ```sh
   acw handoff show HANDOFF_ID
   ```

5. Remain available while the successor reads the document. The startup prompt tells the successor to run `handoff ready` with explicit `--confirm-read` and the frozen document's SHA-256. This is an agent attestation of comprehension/readiness; process liveness alone cannot acknowledge it.
6. Only after `state: "acknowledged"`, retire:

   ```sh
   acw --pane ORIGINAL_PANE handoff retire HANDOFF_ID --session SESSION_ID
   ```

   The original may end its turn while waiting. Once the handoff is acknowledged and the original's pane is idle, the daemon prompts it once through Herdr with this exact command. A working original is left to retire itself, and a blocked one is left for the user. A failed prompt is retried after 30 seconds and recorded as `retire_prompt_error`; a delivered one is recorded as `retire_prompted_at`. Note that the prompt is submitted into the original's input, so unsent text typed there is sent along with it.

**Retirement closes the original Herdr pane.** It verifies the original session, successor identity/liveness, and acknowledgment again. If the successor has itself handed off and retired, retirement follows that chain to the acknowledged successor that is still running; a successor that disappeared any other way keeps the original open. OpenCode panes with multiple internal session tabs are refused to avoid closing unrelated sessions.

Successors use a sibling pane in the same tab/cwd without stealing focus; split direction follows the pane's geometry. Herdr owns all agent startup and pane lifecycle operations. A frozen copy of the handoff is stored outside the repository.

OpenCode creates its fresh session through the original TUI's connected client, preserving model variant, agent profile, location, and explicit session permissions. It then starts the successor with `--session`. Explicit `--server` connections are inherited. Automatic handoff from `--standalone` is rejected because a second TUI cannot discover that private server reliably; monitoring still works there. Mini, headless OpenCode, and native subagents sharing a pane are outside this version's adapter scope.

### Failure recovery

- Startup/prompt failure leaves the original open and records `state: "failed"` plus the error.
- Context reminders resume after a failed attempt and name the failed handoff with its cancel command.
- Repeating `handoff start` returns the existing handoff; it never blindly launches a second successor.
- A valid successor can acknowledge after an ambiguous prompt timeout.
- To abandon a failed/unacknowledged attempt, inspect and close its successor pane through Herdr, then run:

  ```sh
  acw --pane ORIGINAL_PANE handoff cancel HANDOFF_ID --session SESSION_ID
  ```

  A new start is then allowed. Acknowledged handoffs cannot be cancelled this way.
- A daemon restart during preparation/startup marks the attempt failed for inspection. It never retries a launch automatically.
- If the original crashes, an already-started successor can still acknowledge and proceed. If the successor disappears before retirement, retirement is refused.

## Measurement and delivery

| Harness | Context source | Reminder delivery |
| --- | --- | --- |
| OpenCode V2 TUI | Latest completed assistant request in the active context, including cached input, output/reasoning, plus estimated subsequent text/tool results | Idempotent synthetic message through the connected authenticated client, steering at the next step boundary |
| Pi | Native `ctx.getContextUsage()` estimate | Custom steering message, with persisted notification ID for retry deduplication |
| Claude Code | Latest main-thread transcript assistant usage, cached input and output, plus estimated trailing user/tool text | Hook `additionalContext`; Stop uses one feedback turn to let the agent assess the first reminder; repeat reminders are held at Stop and delivered on the next prompt or tool call |

All three readings are labelled **estimated**. Input counts are anchored in the last provider request, but tool output since that request, images, provider-hidden context, and in-flight generation prevent an exact next-request count. Text additions use a four-characters-per-token approximation. No cumulative session counters are used. After compaction with no new provider reading, usage is unknown rather than reused from the old context.

OpenCode and Pi sample every five seconds; Pi also samples at turn/model/compaction boundaries. OpenCode monitors the current session route in each TUI. Claude reports on hook boundaries; an idle Claude session can appear stale even though it is healthy. Missing telemetry means an adapter is absent/not loaded, not zero usage.

## State and local protocol

- Configuration: `~/.config/agent-context-watchdog/config.json`.
- State, log, and frozen handoffs: `~/.local/state/agent-context-watchdog/<Herdr-socket-hash>/`.
- IPC: user-private Unix socket in `$XDG_RUNTIME_DIR`, falling back to the scoped state directory.
- `ACW_HOME` overrides the config/state root for isolated testing; use the same environment for the daemon and adapters.

State is persisted in SQLite. The daemon uses a single-instance lock and serializes policy changes. It discovers only the Herdr session named by `HERDR_SOCKET_PATH`; adapters outside Herdr are inert. Initial adapter reports are checked against the pane's foreground process. Commands trust the local user; the private socket is not an authentication boundary between processes running as that user.

No transcripts or credentials are sent to the daemon. The Claude hook reads its transcript locally and sends only usage/identity metadata. Handoff documents are retained until manually removed; no automatic deletion is performed. Reminder IDs, observations, and handoffs survive daemon restarts.

## Tests

```sh
python3 -m unittest discover -s tests -v
bun test tests/adapters.test.mjs
```

Tests cover native adapter payloads, current-vs-cumulative accounting, cached tokens, compaction, model overrides, live config reload, notification retries, daemon restart, real Unix-socket communication, and acknowledged handoff ownership/failure paths. Herdr lifecycle commands are exercised through a fake; tests do not launch billable agents.

## Remove

Run `acw stop` in each monitored Herdr session. Remove this checkout's plugin entry from OpenCode's `cli.json`, the `context-watchdog.ts` Pi extension, and hook handlers whose command ends in this checkout's `bin/acw claude-hook` from Claude settings. Then remove the `~/.local/bin/acw` symlink. Keep or delete the dedicated config/state directories as desired. JSON backups are adjacent to files changed by installation.

## Integration references

- [OpenCode V2 CLI plugin API](https://opencode.ai/v2/docs/build/plugins/cli), [HTTP API](https://opencode.ai/v2/docs/api), [CLI configuration](https://opencode.ai/v2/docs/cli/config)
- [Pi extension API](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/types.ts)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- Installed Herdr CLI help is the authority for lifecycle commands.
