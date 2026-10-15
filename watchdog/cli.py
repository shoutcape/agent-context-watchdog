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
    for name in ("start", "watch", "stop", "status", "config", "claude-hook"):
        sub.add_parser(name)
    setup = sub.add_parser("install", help="Install CLI link and harness adapters")
    setup.add_argument("--harness", choices=["all", "opencode", "pi", "claude"], default="all")
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


def main():
    args = parser().parse_args()
    try:
        if args.command == "install":
            from .install import install
            print(json.dumps(install(args.harness, command_path()), indent=2))
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
