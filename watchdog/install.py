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
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + f".acw-backup-{time.time_ns()}"))
    temporary = path.with_name(path.name + ".acw-tmp")
    temporary.write_text(updated)
    temporary.chmod(0o600)
    temporary.replace(path)


def install(harness, command):
    root = Path(__file__).resolve().parents[1]
    if not (root / "adapters").is_dir():
        raise ValueError("Run install from the source checkout's bin/acw")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    bin_path = Path.home() / ".local/bin/acw"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    if bin_path.exists() or bin_path.is_symlink():
        if bin_path.resolve() != Path(command).resolve():
            raise ValueError(f"Refusing to replace unrelated executable: {bin_path}")
    else:
        bin_path.symlink_to(command)
    installed = [str(bin_path)]
    if harness in ("all", "opencode"):
        path = config_home / "opencode/cli.json"
        package = str(root / "adapters/opencode")

        def add_plugin(data):
            plugins = data.setdefault("plugins", [])
            if not any(p == package or (isinstance(p, dict) and p.get("package") == package) for p in plugins):
                plugins.append(package)

        update_json(path, add_plugin)
        installed.append(str(path))
    if harness in ("all", "pi"):
        path = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi/agent")) / "extensions/context-watchdog.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"// Managed by agent-context-watchdog\nexport {{ default }} from {json.dumps(str(root / 'adapters/pi/runtime.mjs'))};\n"
        if path.exists() and not path.read_text().startswith("// Managed by agent-context-watchdog\n"):
            raise ValueError(f"Refusing to overwrite unrelated extension: {path}")
        path.write_text(content)
        installed.append(str(path))
    if harness in ("all", "claude"):
        path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
        hook_command = shlex.join([command, "claude-hook"])

        def add_hooks(data):
            hooks = data.setdefault("hooks", {})
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"):
                groups = hooks.setdefault(event, [])
                if not any(h.get("command") == hook_command for g in groups for h in g.get("hooks", [])):
                    groups.append({"hooks": [{"type": "command", "command": hook_command, "timeout": 10}]})

        update_json(path, add_hooks)
        installed.append(str(path))
    return {"installed": installed, "next": "Run acw config, then acw start inside Herdr. Restart harnesses or reload their extensions."}
