import hashlib
import threading
import time
import uuid

from .config import Config


KINDS = {"opencode", "pi", "claude"}


def observation_key(pane, terminal, session):
    return hashlib.sha256(f"{pane}\0{terminal}\0{session}".encode()).hexdigest()


def reminder(observation, command, failed=None):
    import shlex
    prefix = [command, "--pane", observation["pane"]]
    retry = ""
    if failed:
        cancel = shlex.join([*prefix, "handoff", "cancel", failed["id"], "--session", observation["session"]])
        retry = (f"Your previous handoff {failed['id']} failed: {failed.get('error', 'unknown error')}. "
                 "Starting again returns that failed attempt. To retry, inspect its successor pane, "
                 f"close it through Herdr if it exists, then run: {cancel}. ")
    start = shlex.join([*prefix, "handoff", "start", "--session", observation["session"],
                        "--document", "/absolute/path/to/handoff.md", "--quiesced"])
    return (
        f"[Agent context watchdog] Current context: {observation['tokens']:,} tokens "
        f"({observation['quality']}); advisory threshold: {observation['limit']:,} "
        f"for {observation['model']} (rule: {observation['rule']}). "
        "Decide whether you can finish the remaining work here. If so, finish normally. "
        "If unfinished work needs a fresh agent, write a handoff document covering the goal, "
        "constraints, working directory, changes, checks, decisions, blockers, and next steps. "
        "Stop modifying shared files, then start your successor through Herdr using: "
        f"{start}. Replace the document path. This returns a handoff ID. "
        f"Use {shlex.join(prefix)} handoff show ID to check acknowledgment. "
        "Remain available until the successor explicitly confirms reading the handoff and "
        f"being ready; only then use {shlex.join(prefix)} handoff retire ID --session "
        f"{shlex.quote(observation['session'])}. This closes your original pane. "
        "If you end your turn before that, the watchdog prompts you once the successor confirms. "
        "If startup or acknowledgment fails, stay open. Do not create another successor blindly. "
        f"{retry}"
    ).rstrip()


class Core:
    def __init__(self, store, herdr, config_path, command, clock=time.time):
        self.store, self.herdr, self.config_path = store, herdr, config_path
        self.command, self.clock = command, clock
        self.lock = threading.RLock()
        self.config = Config.load(config_path)
        self.config_error = None
        self.agents = {}
        self.discovery_error = None
        self.last_discovery = 0
        self.preparations = {}

    def refresh(self, force=False):
        with self.lock:
            try:
                self.config = Config.load(self.config_path)
                self.config_error = None
            except (OSError, ValueError, TypeError) as exc:
                self.config_error = str(exc)
            if not force and self.clock() - self.last_discovery < self.config.discovery_interval_seconds:
                return
            self.last_discovery = self.clock()
            try:
                # A name can outlive its agent; such panes carry no agent label.
                self.agents = {pane: agent for pane, agent in self.herdr.agents().items()
                               if agent.get("agent")}
                self.discovery_error = None
            except Exception as exc:
                self.agents = {}
                self.discovery_error = str(exc)

    def validate_caller(self, request, kind=None, terminal=None):
        self.refresh(force=True)
        agent = self.agents.get(request.get("pane"))
        if not agent or agent["agent"] not in KINDS:
            raise ValueError("Pane does not currently contain a supported Herdr-managed agent")
        if kind and agent["agent"] != kind:
            raise ValueError("Harness identity changed")
        if terminal and agent["terminal_id"] != terminal:
            raise ValueError("Pane occupant changed")
        return self.herdr.verify_process(request["pane"], request.get("pids", []))

    def observe(self, request):
        with self.lock:
            self.refresh()
            agent = self.agents.get(request.get("pane"))
            if not agent or agent["agent"] != request.get("kind"):
                self.refresh(force=True)
                agent = self.agents.get(request.get("pane"))
            if not agent or agent["agent"] != request.get("kind") or agent["agent"] not in KINDS:
                return {"ignored": "not a matching live Herdr agent"}
            session, model = request.get("session"), request.get("model")
            if not isinstance(session, str) or not session or not isinstance(model, str) or not model:
                raise ValueError("session and model must be nonempty strings")
            tokens = request.get("tokens")
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError("tokens must be a nonnegative integer or null")
            if request.get("quality") not in ("reported", "estimated"):
                raise ValueError("quality must be reported or estimated")
            key = observation_key(request["pane"], agent["terminal_id"], session)
            observations = self.store.data["observations"]
            previous = observations.get(key, {})
            # Store only the validated foreground PID: hook processes are new each time,
            # but a report from beneath the same harness needs no Herdr round trip.
            pids = previous.get("pids", [])
            if not previous.get("active") or not set(pids).intersection(request.get("pids", [])):
                pids = self.validate_caller(request, request["kind"])
            for item in observations.values():
                if item["pane"] == request["pane"] and item["session"] != session:
                    item["active"] = False
            limit, rule = self.config.limit(model)
            reset = previous.get("model") != model or previous.get("limit") != limit
            if tokens is not None and tokens < limit * self.config.rearm_ratio:
                reset = True
            if reset:
                previous = {**previous, "notified_tokens": None, "pending": None}
            observation = {
                **previous,
                "key": key, "pane": request["pane"], "terminal": agent["terminal_id"],
                "kind": request["kind"], "session": session, "model": model,
                "tokens": tokens, "quality": request["quality"], "limit": limit, "rule": rule,
                "observed_at": self.clock(), "active": True, "pids": pids,
                "cwd": request.get("cwd") or agent.get("foreground_cwd") or agent["cwd"],
                "profile": request.get("profile"), "capacity": request.get("capacity"),
                "can_retire": request.get("can_retire", True),
                "server_args": request.get("server_args", []),
                "standalone": request.get("standalone", False),
            }
            observations[key] = observation
            handoffs = [h for h in self.store.data["handoffs"].values() if h["source_key"] == key]
            # A failed attempt leaves the original working, so it still needs reminders.
            in_handoff = any(h["state"] not in ("cancelled", "failed") for h in handoffs)
            failed = next((h for h in handoffs if h["state"] == "failed"), None)
            last = observation.get("notified_tokens")
            due = tokens is not None and tokens >= limit and (
                last is None or (self.config.remind_every_tokens > 0
                                 and tokens >= last + self.config.remind_every_tokens)
            )
            if due and not in_handoff and not observation.get("pending"):
                observation["pending"] = {
                    "id": "msg_acw" + uuid.uuid4().hex,
                    "text": reminder(observation, self.command, failed), "tokens": tokens,
                    "repeat": last is not None,
                }
            if tokens is None:
                # Never inject an old, pre-compaction reading as current usage.
                observation["pending"] = None
            elif tokens < limit:
                observation["pending"] = None
            pending = observation.get("pending")
            notification = None
            if pending and not in_handoff and pending.get("lease_until", 0) <= self.clock():
                pending["lease_until"] = self.clock() + 15
                notification = dict(pending)
            self.store.save()
            prepare = next(({"id": h["id"], "session": h["successor_session"]}
                            for h in self.store.data["handoffs"].values()
                            if h["source_key"] == key and h["state"] == "preparing"), None)
            return {"key": key, "notification": notification, "prepare": prepare}

    def prepared(self, request):
        with self.lock:
            handoff = self.store.data["handoffs"][request["id"]]
            if request["pane"] != handoff["source_pane"] or handoff["state"] != "preparing":
                raise ValueError("No pending preparation for this pane")
            if request.get("error"):
                handoff["preparation_error"] = request["error"]
            else:
                handoff["prepared"] = True
            self.store.save()
            self.preparations[handoff["id"]].set()
            return {"ok": True}

    def delivered(self, request):
        with self.lock:
            observation = self.store.data["observations"][request["key"]]
            if observation["pane"] != request["pane"]:
                raise ValueError("Notification belongs to another pane")
            pending = observation.get("pending")
            if pending and pending["id"] == request["id"]:
                observation["notified_tokens"] = pending["tokens"]
                observation["pending"] = None
                self.store.save()
            return {"delivered": True}

    def source(self, request):
        self.refresh(force=True)
        agent = self.agents.get(request["pane"])
        if not agent:
            raise ValueError("Original agent is no longer present")
        key = observation_key(request["pane"], agent["terminal_id"], request["session"])
        observation = self.store.data["observations"].get(key)
        if not observation or not observation["active"]:
            raise ValueError("No active telemetry for this session; enable its adapter first")
        if self.clock() - observation["observed_at"] > self.config.stale_after_seconds:
            raise ValueError("Session telemetry is stale; wait for the adapter to report")
        # OpenCode shell tools run in its shared server, not beneath the TUI.
        # Validate the adapter's foreground process, not the tool process ancestry.
        self.validate_caller({**request, "pids": observation["pids"]}, observation["kind"], observation["terminal"])
        return observation

    def status(self):
        with self.lock:
            self.refresh(force=True)
            rows = []
            for pane, agent in self.agents.items():
                if agent["agent"] not in KINDS:
                    continue
                observations = [o for o in self.store.data["observations"].values()
                                if o["pane"] == pane and o["terminal"] == agent["terminal_id"] and o["active"]]
                if not observations:
                    rows.append({"pane": pane, "kind": agent["agent"], "telemetry": "missing"})
                for observation in observations:
                    age = self.clock() - observation["observed_at"]
                    state = "stale" if age > self.config.stale_after_seconds else "current"
                    if observation["tokens"] is None:
                        state = "unknown"
                    row = {k: v for k, v in observation.items() if k not in ("pids", "pending")}
                    row["telemetry"] = state
                    if observation.get("capacity") and observation["limit"] >= observation["capacity"]:
                        row["warning"] = "Threshold meets/exceeds model capacity; native compaction may occur first"
                    rows.append(row)
            return {"agents": rows, "config_error": self.config_error,
                    "discovery_error": self.discovery_error,
                    "handoffs": list(self.store.data["handoffs"].values())}
