import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .config import Config, DEFAULTS, Paths, caller
from .server import rpc, serve


def command_path():
    local = Path(__file__).resolve().parents[1] / "bin/acw"
    return str(local if local.exists() else Path(sys.argv[0]).resolve())


def parser():
    root = argparse.ArgumentParser(description="Herdr agent context watchdog")
    root.add_argument("--pane", help="Explicit caller pane (needed for shared-server harness tools)")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("start", "watch", "stop", "config", "claude-hook"):
        sub.add_parser(name)
    sub.add_parser("status").add_argument("--verbose", "-v", action="store_true", help="Print the full JSON status")
    setup = sub.add_parser("install", help="Install CLI link and harness adapters")
    setup.add_argument("--harness", choices=["all", "opencode", "pi", "claude"], default="all")
    remove = sub.add_parser("uninstall", help="Remove CLI link and harness adapters")
    remove.add_argument("--harness", choices=["all", "opencode", "pi", "claude"], default="all")
    handoff = sub.add_parser("handoff").add_subparsers(dest="action", required=True)
    start = handoff.add_parser("start")
    start.add_argument("--session", required=True)
    start.add_argument("--document", required=True)
    start.add_argument("--quiesced", action="store_true")
    ready = handoff.add_parser("ready")
    ready.add_argument("id")
    ready.add_argument("--confirm-read", action="store_true")
    ready.add_argument("--sha256", required=True)
    for action in ("retire", "cancel"):
        cmd = handoff.add_parser(action)
        cmd.add_argument("id")
        cmd.add_argument("--session", required=True)
    handoff.add_parser("show").add_argument("id")
    return root


def tokens(count):
    return f"{count / 1000:.0f}k" if count >= 1000 else str(count)


def format_status(status, now=None):
    now = time.time() if now is None else now
    agents = status["agents"]
    lines = [f"Watchdog running · {len(agents)} agent{'' if len(agents) == 1 else 's'}"]
    rows = []
    for agent in agents:
        if agent["telemetry"] == "missing":
            rows.append((agent["pane"], agent["kind"], "", "no telemetry (adapter not installed?)"))
            continue
        if agent["tokens"] is None:
            usage = "context unknown"
        else:
            estimate = "~" if agent["quality"] == "estimated" else ""
            usage = (f"{estimate}{tokens(agent['tokens'])} / {tokens(agent['limit'])}"
                     f"  {agent['tokens'] * 100 // agent['limit']}%")
        if agent["telemetry"] == "stale":
            usage += f"  stale {int(now - agent['observed_at']) // 60}m"
        if agent.get("warning"):
            usage += "  ! limit ≥ model capacity"
        rows.append((agent["pane"], agent["kind"], agent["model"], usage))
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(3)]
    lines += ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)) + "  " + row[3] for row in rows]
    handoffs = [h for h in status["handoffs"] if h["state"] not in ("retired", "cancelled")]
    if handoffs:
        lines.append("Handoffs")
        for handoff in handoffs:
            successor = handoff.get("successor_pane") or "…"
            lines.append(f"  {handoff['id']}  {handoff['source_pane']} → {successor}  {handoff['state']}")
    for key, label in (("config_error", "Config error"), ("discovery_error", "Discovery error")):
        if status.get(key):
            lines.append(f"{label}: {status[key]}")
    return "\n".join(lines)


def main():
    args = parser().parse_args()
    try:
        if args.command == "install":
            from .install import install
            print(json.dumps(install(args.harness, command_path()), indent=2))
            return
        if args.command == "uninstall":
            from .install import uninstall
            print(json.dumps(uninstall(args.harness, command_path()), indent=2))
            return
        if args.command == "claude-hook" and os.environ.get("HERDR_ENV") != "1":
            return
        paths = Paths()
        if args.command == "watch":
            serve(paths, command_path())
            return
        if args.command == "start":
            try:
                rpc(paths, {"op": "status"})
                print("Watchdog is already running.")
                return
            except (OSError, ValueError, RuntimeError):
                pass
            Config.load(paths.config)  # Fail before launching on malformed config.
            log_path = paths.state / "daemon.log"
            with log_path.open("a") as log:
                process = subprocess.Popen(
                    [sys.executable, command_path(), "watch"], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True, cwd=str(Path.home()),
                )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Watchdog exited; see {log_path}")
                try:
                    rpc(paths, {"op": "status"}, timeout=1)
                    print(f"Watchdog running. Log: {log_path}")
                    return
                except (OSError, ValueError, RuntimeError):
                    time.sleep(0.1)
            raise RuntimeError(f"Startup not confirmed; inspect {log_path}")
        if args.command == "config":
            if not paths.config.exists():
                paths.config.parent.mkdir(parents=True, exist_ok=True)
                paths.config.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
            Config.load(paths.config)
            print(paths.config)
            return
        if args.command == "claude-hook":
            from .claude import run_hook
            run_hook(paths, json.load(sys.stdin))
            return
        if args.command in ("status", "stop"):
            result = rpc(paths, {"op": args.command}, timeout=20)
        else:
            request = {**caller(), **vars(args), "op": f"handoff.{args.action}"}
            request["pane"] = args.pane or caller()["pane"]
            if args.action == "start":
                request["document"] = str(Path(args.document).expanduser().resolve())
            result = rpc(paths, request, timeout=120)
        if args.command == "status" and not args.verbose:
            print(format_status(result))
            return
        print(json.dumps(result, indent=2))
        if isinstance(result, dict) and result.get("state") == "failed":
            sys.exit(1)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        # Hooks must never break an agent turn if the watchdog is unavailable.
        if args.command == "claude-hook":
            print(f"acw hook: {exc}", file=sys.stderr)
            return
        print(f"acw: {exc}", file=sys.stderr)
        sys.exit(1)
