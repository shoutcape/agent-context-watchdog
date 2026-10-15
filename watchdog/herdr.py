import json
import os
import subprocess


class Herdr:
    def __init__(self):
        if os.environ.get("HERDR_ENV") != "1":
            raise ValueError("Herdr context required")

    def run(self, *args, timeout=15):
        result = subprocess.run(
            [os.environ.get("HERDR_BIN_PATH", "herdr"), *args],
            text=True, capture_output=True, timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"Herdr {' '.join(args[:2])}: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if "error" in payload:
            raise RuntimeError(str(payload["error"]))
        return payload["result"]

    def agents(self):
        return {a["pane_id"]: a for a in self.run("agent", "list")["agents"]}

    def verify_process(self, pane, pids):
        info = self.run("pane", "process-info", "--pane", pane)["process_info"]
        matched = {p["pid"] for p in info["foreground_processes"]}.intersection(pids)
        if not matched:
            raise ValueError("Caller is not the foreground agent in this pane")
        return sorted(matched)

    def split(self, pane, cwd):
        layout = self.run("pane", "layout", "--pane", pane)["layout"]
        rect = next(p["rect"] for p in layout["panes"] if p["pane_id"] == pane)
        direction = "right" if rect["width"] >= 2 * rect["height"] else "down"
        return self.run(
            "pane", "split", "--pane", pane, "--direction", direction,
            "--cwd", cwd, "--no-focus",
        )["pane"]["pane_id"]

    def start(self, name, kind, pane, args):
        return self.run("agent", "start", name, "--kind", kind, "--pane", pane,
                        "--", *args, timeout=45)

    def prompt(self, pane, text):
        return self.run("agent", "prompt", pane, text)

    def close(self, pane):
        return self.run("pane", "close", pane)
