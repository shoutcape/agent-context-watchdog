import { Plugin } from "@opencode/plugin/tui";
import { start } from "./runtime.mjs";

export default Plugin.define({
  id: "agent-context-watchdog",
  setup: start,
});
