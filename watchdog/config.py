from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import os


DEFAULTS = {
    "global_limit": 200_000,
    "model_limits": {"claude-haiku-5-5": 100_000},
    "remind_every_tokens": 20_000,
    "rearm_ratio": 0.9,
    "stale_after_seconds": 120,
    "discovery_interval_seconds": 5,
    "launch_args": {"opencode": [], "pi": [], "claude": []},
}


@dataclass
class Config:
    global_limit: int = 200_000
    model_limits: dict = field(default_factory=lambda: {"claude-haiku-5-5": 100_000})
    remind_every_tokens: int = 20_000
    rearm_ratio: float = 0.9
    stale_after_seconds: int = 120
    discovery_interval_seconds: int = 5
    launch_args: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict) or set(data) - set(DEFAULTS):
            raise ValueError("Unknown configuration fields or invalid configuration object")
        config = cls(**data)
        for name in ("global_limit", "stale_after_seconds", "discovery_interval_seconds"):
            value = getattr(config, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(config.remind_every_tokens) is not int or config.remind_every_tokens < 0:
            raise ValueError("remind_every_tokens must be nonnegative (0 disables repeats)")
        if type(config.rearm_ratio) not in (int, float) or not 0 < config.rearm_ratio < 1:
            raise ValueError("rearm_ratio must be between 0 and 1")
        if not isinstance(config.model_limits, dict) or any(
            not isinstance(k, str) or not k or type(v) is not int or v <= 0
            for k, v in config.model_limits.items()
        ):
            raise ValueError("model_limits must map exact model IDs to positive integers")
        if not isinstance(config.launch_args, dict) or any(
            k not in ("opencode", "pi", "claude") or not isinstance(v, list)
            or any(not isinstance(arg, str) for arg in v)
            for k, v in config.launch_args.items()
        ):
            raise ValueError("launch_args must map harness names to argument arrays")
        reserved = {"--continue", "-c", "--resume", "-r", "--session", "-s", "--model", "-m", "--standalone"}
        for args in config.launch_args.values():
            if any(arg.split("=", 1)[0] in reserved for arg in args):
                raise ValueError("launch_args cannot override fresh-session/model selection or use --standalone")
        return config

    def limit(self, model: str):
        # Provider-qualified exact match wins; then the final model ID; then fallback.
        for key in (model, model.rsplit("/", 1)[-1]):
            if key in self.model_limits:
                return self.model_limits[key], key
        return self.global_limit, "global"


class Paths:
    def __init__(self):
        self.herdr_socket = os.environ.get("HERDR_SOCKET_PATH", "")
        if os.environ.get("HERDR_ENV") != "1" or not self.herdr_socket:
            raise ValueError("Run acw inside a Herdr-managed pane (HERDR_ENV=1)")
        scope = hashlib.sha256(self.herdr_socket.encode()).hexdigest()[:16]
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        root = os.environ.get("ACW_HOME")
        self.config = Path(root) / "config.json" if root else config_home / "agent-context-watchdog/config.json"
        self.state = (Path(root) if root else state_home / "agent-context-watchdog") / scope
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", self.state))
        self.socket = runtime / f"acw-{scope}.sock"
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.state / "state.sqlite3"


def caller():
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise ValueError("HERDR_PANE_ID is missing")
    # Hooks may be grandchildren of the foreground harness process.
    ancestors = []
    pid = os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            pid = int(stat[1])
        except (OSError, ValueError, IndexError):
            break
    return {"pane": pane, "pids": ancestors}
