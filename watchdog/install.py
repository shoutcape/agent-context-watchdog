from pathlib import Path
import json
import os
import shlex
import shutil
import time


def update_json(path, change):
    original = path.read_text() if path.exists() else "{}"
    data = json.loads(original)
    change(data)
    updated = json.dumps(data, indent=2) + "\n"
    if json.loads(original) == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + f".acw-backup-{time.time_ns()}"))
    temporary = path.with_name(path.name + ".acw-tmp")
    temporary.write_text(updated)
    temporary.chmod(0o600)
    temporary.replace(path)
    return True


CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop")
PI_HEADER = "// Managed by agent-context-watchdog\n"


def targets(command):
    root = Path(__file__).resolve().parents[1]
    if not (root / "adapters").is_dir():
        raise ValueError("Run install from the source checkout's bin/acw")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return {
        "root": root,
        "bin": Path.home() / ".local/bin/acw",
        "opencode": config_home / "opencode/cli.json",
        "opencode_package": str(root / "adapters/opencode"),
        "pi": Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi/agent")) / "extensions/context-watchdog.ts",
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json",
        "claude_command": shlex.join([command, "claude-hook"]),
    }


def install(harness, command):
    t = targets(command)
    root = t["root"]
    bin_path = t["bin"]
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    if bin_path.exists() or bin_path.is_symlink():
        if bin_path.resolve() != Path(command).resolve():
            raise ValueError(f"Refusing to replace unrelated executable: {bin_path}")
    else:
        bin_path.symlink_to(command)
    installed = [str(bin_path)]
    if harness in ("all", "opencode"):
        path, package = t["opencode"], t["opencode_package"]

        def add_plugin(data):
            plugins = data.setdefault("plugins", [])
            if not any(p == package or (isinstance(p, dict) and p.get("package") == package) for p in plugins):
                plugins.append(package)

        update_json(path, add_plugin)
        installed.append(str(path))
    if harness in ("all", "pi"):
        path = t["pi"]
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"{PI_HEADER}export {{ default }} from {json.dumps(str(root / 'adapters/pi/runtime.mjs'))};\n"
        if path.exists() and not path.read_text().startswith(PI_HEADER):
            raise ValueError(f"Refusing to overwrite unrelated extension: {path}")
        path.write_text(content)
        installed.append(str(path))
    if harness in ("all", "claude"):
        path, hook_command = t["claude"], t["claude_command"]

        def add_hooks(data):
            hooks = data.setdefault("hooks", {})
            for event in CLAUDE_EVENTS:
                groups = hooks.setdefault(event, [])
                if not any(h.get("command") == hook_command for g in groups for h in g.get("hooks", [])):
                    groups.append({"hooks": [{"type": "command", "command": hook_command, "timeout": 10}]})

        update_json(path, add_hooks)
        installed.append(str(path))
    return {"installed": installed, "next": "Run acw config, then acw start inside Herdr. Restart harnesses or reload their extensions."}


def uninstall(harness, command):
    t = targets(command)
    removed = []
    if harness in ("all", "opencode") and t["opencode"].exists():
        package = t["opencode_package"]

        def remove_plugin(data):
            plugins = data.get("plugins")
            if not isinstance(plugins, list):
                return
            plugins[:] = [p for p in plugins
                          if not (p == package or (isinstance(p, dict) and p.get("package") == package))]
            if not plugins:
                del data["plugins"]

        if update_json(t["opencode"], remove_plugin):
            removed.append(str(t["opencode"]))
    if harness in ("all", "pi") and t["pi"].exists():
        # Leave an extension we did not write alone.
        if t["pi"].read_text().startswith(PI_HEADER):
            t["pi"].unlink()
            removed.append(str(t["pi"]))
    if harness in ("all", "claude") and t["claude"].exists():
        def remove_hooks(data):
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return
            for event in CLAUDE_EVENTS:
                groups = hooks.get(event)
                if not isinstance(groups, list):
                    continue
                kept = []
                for group in groups:
                    handlers = group.get("hooks")
                    if isinstance(handlers, list):
                        remaining = [h for h in handlers if h.get("command") != t["claude_command"]]
                        # Drop only groups emptied here, not ones that were already empty.
                        if handlers and not remaining:
                            continue
                        group["hooks"] = remaining
                    kept.append(group)
                groups[:] = kept
                if not groups:
                    del hooks[event]
            if not hooks:
                del data["hooks"]

        if update_json(t["claude"], remove_hooks):
            removed.append(str(t["claude"]))
    # The CLI link serves every harness, so only a full uninstall removes it.
    if harness == "all" and t["bin"].is_symlink() and t["bin"].resolve() == Path(command).resolve():
        t["bin"].unlink()
        removed.append(str(t["bin"]))
    return {"removed": removed,
            "next": "Run bin/acw stop in each monitored Herdr session. Restart harnesses or reload their extensions. "
                    "Config and state directories are kept; JSON backups sit beside changed files."}
