from pathlib import Path
import hashlib
import shlex
import threading
import uuid


RETIRE_PROMPT_RETRY_SECONDS = 30


def retire_prompt(handoff, command):
    retire = shlex.join([command, "--pane", handoff["source_pane"], "handoff", "retire",
                         handoff["id"], "--session", handoff["source_session"]])
    return (
        f"[Agent context watchdog] Your successor in pane {handoff['successor_pane']} acknowledged "
        f"handoff {handoff['id']} and has taken over the work. Do not modify shared files. "
        f"Retire now by running: {retire}. This closes your pane. "
        "If you must stay open (for example, the user asked you to), say why instead."
    )


class Handoffs:
    def __init__(self, core, directory):
        self.core = core
        self.directory = directory

    def get(self, ident):
        try:
            return self.core.store.data["handoffs"][ident]
        except KeyError:
            raise ValueError("Unknown handoff ID") from None

    def save(self, handoff, **changes):
        with self.core.lock:
            handoff.update(changes)
            self.core.store.save()

    def start(self, request):
        if request.get("quiesced") is not True:
            raise ValueError("Stop modifying shared files, then pass --quiesced")
        document = Path(request["document"])
        if not document.is_absolute() or not document.is_file():
            raise ValueError("Handoff document must be an existing absolute file path")
        content = document.read_bytes()
        if not content.strip() or len(content) > 2_000_000:
            raise ValueError("Handoff document must be nonempty and at most 2 MB")
        content.decode("utf-8")
        with self.core.lock:
            source = self.core.source(request)
            if not source["can_retire"]:
                raise ValueError("Close other harness session tabs before handing off this pane")
            if source.get("standalone"):
                raise ValueError("Automatic OpenCode succession requires a shared or explicit --server connection, not --standalone")
            for handoff in self.core.store.data["handoffs"].values():
                if handoff["source_key"] == source["key"] and handoff["state"] != "cancelled":
                    # Retrying the same intent never launches a duplicate successor.
                    return handoff
            ident = uuid.uuid4().hex
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            snapshot = self.directory / f"{ident}.md"
            snapshot.write_bytes(content)
            snapshot.chmod(0o600)
            handoff = {
                "id": ident, "source_key": source["key"], "source_pane": source["pane"],
                "source_session": source["session"], "source_terminal": source["terminal"],
                "kind": source["kind"], "model": source["model"], "cwd": source["cwd"],
                "document": str(snapshot), "sha256": hashlib.sha256(content).hexdigest(),
                "state": "preparing" if source["kind"] == "opencode" else "starting", "created_at": self.core.clock(),
                "successor_name": f"acw-{ident[:12]}",
            }
            self.core.store.data["handoffs"][ident] = handoff
            if source["kind"] == "opencode":
                handoff["successor_session"] = "ses_acw" + ident
                self.core.preparations[ident] = threading.Event()
            self.core.store.save()
            args = list(self.core.config.launch_args.get(source["kind"], []))
            if source["kind"] == "opencode":
                args += source.get("server_args", [])
                args += ["--session", handoff["successor_session"]]
            else:
                args += ["--model", source["model"]]
        # Long-running Herdr commands must not hold the telemetry/acknowledgment lock.
        try:
            if source["kind"] == "opencode":
                if not self.core.preparations[ident].wait(30) or not handoff.get("prepared"):
                    raise RuntimeError(handoff.get("preparation_error", "Original OpenCode adapter did not prepare a fresh session"))
                self.save(handoff, state="starting")
            pane = self.core.herdr.split(source["pane"], source["cwd"])
            self.save(handoff, successor_pane=pane)
            self.core.herdr.start(handoff["successor_name"], source["kind"], pane, args)
            successor = self.core.herdr.agents().get(pane)
            if not successor or successor.get("agent") != source["kind"] or successor.get("name") != handoff["successor_name"]:
                raise RuntimeError("Herdr did not confirm the expected successor identity")
            self.save(handoff, successor_terminal=successor["terminal_id"], state="awaiting_ack")
            ack = shlex.join([self.core.command, "--pane", pane, "handoff", "ready", ident,
                              "--confirm-read", "--sha256", handoff["sha256"]])
            prompt = (
                f"You are the successor for handoff {ident}. Read the complete handoff document at "
                f"{snapshot}. Original agent: {source['pane']}; working directory: {source['cwd']}. "
                "The original has stopped changing shared files. Verify that you understand the goal, "
                "constraints and next steps. Before modifying shared files, explicitly acknowledge "
                f"that you have read the document and are ready by running: {ack}. "
                "Only continue the work after that command succeeds. If you cannot take over, "
                "do not acknowledge; explain the blocker. The original will remain available. "
                "Follow the handoff's user instructions and applicable repository instructions."
            )
            self.core.herdr.prompt(pane, prompt)
            return handoff
        except Exception as exc:
            with self.core.lock:
                # A prompt timeout may occur after a successful acknowledgment.
                if handoff["state"] != "acknowledged":
                    self.save(handoff, state="failed", error=str(exc))
            return handoff

    def ready(self, request):
        if request.get("confirm_read") is not True:
            raise ValueError("Read the handoff, then explicitly pass --confirm-read")
        with self.core.lock:
            handoff = self.get(request["id"])
            if request["pane"] != handoff.get("successor_pane"):
                raise ValueError("Only the designated successor can acknowledge")
            if handoff["state"] not in ("awaiting_ack", "failed", "acknowledged"):
                raise ValueError("Successor startup is not ready for acknowledgment")
            if not handoff.get("successor_terminal"):
                raise ValueError("Successor identity was never confirmed by Herdr")
            content = Path(handoff["document"]).read_bytes()
            if request.get("sha256") != handoff["sha256"] or hashlib.sha256(content).hexdigest() != handoff["sha256"]:
                raise ValueError("Handoff digest does not match the frozen document")
            self.core.refresh(force=True)
            candidates = [o for o in self.core.store.data["observations"].values()
                          if o["pane"] == request["pane"] and o["active"]
                          and o["terminal"] == handoff["successor_terminal"]]
            if len(candidates) != 1:
                raise ValueError("Successor telemetry is not registered; enable its adapter and retry")
            observation = self.core.source({**request, "session": candidates[0]["session"]})
            agent = self.core.agents[request["pane"]]
            if agent.get("name") != handoff["successor_name"] or observation["kind"] != handoff["kind"]:
                raise ValueError("Successor identity changed")
            self.save(handoff, state="acknowledged", acknowledged_at=self.core.clock(),
                      successor_session=observation["session"])
            return {"ready": True, "id": handoff["id"], "message": "Takeover acknowledged; you may continue the work."}

    def prompt_retirements(self):
        # An original that ended its turn before the acknowledgment arrived never polls
        # again. Wake it once through Herdr so retirement does not depend on timing.
        with self.core.lock:
            self.core.refresh()
            due = []
            for handoff in self.core.store.data["handoffs"].values():
                if handoff["state"] != "acknowledged" or handoff.get("retire_prompted_at"):
                    continue
                if handoff.get("retire_prompt_retry_at", 0) > self.core.clock():
                    continue
                agent = self.core.agents.get(handoff["source_pane"])
                if not agent or agent["terminal_id"] != handoff["source_terminal"] or agent["agent"] != handoff["kind"]:
                    continue  # The original is gone or was replaced; there is nothing to retire.
                # A working original can still retire itself; a blocked one is waiting on the user.
                if agent.get("agent_status") in ("idle", "done"):
                    due.append(handoff)
        # Herdr prompts can take seconds; never hold the telemetry lock across them.
        for handoff in due:
            try:
                self.core.herdr.prompt(handoff["source_pane"], retire_prompt(handoff, self.core.command))
            except Exception as exc:
                self.save(handoff, retire_prompt_error=str(exc),
                          retire_prompt_retry_at=self.core.clock() + RETIRE_PROMPT_RETRY_SECONDS)
            else:
                self.save(handoff, retire_prompted_at=self.core.clock(), retire_prompt_error=None)

    def retire(self, request):
        with self.core.lock:
            handoff = self.get(request["id"])
            if handoff["state"] != "acknowledged":
                raise ValueError("Original must remain open until explicit successor acknowledgment")
            source = self.core.source(request)
            if source["key"] != handoff["source_key"] or not source["can_retire"]:
                raise ValueError("Original identity changed or additional session tabs are open")
            heir = self.live_heir(handoff)
            if not heir:
                raise ValueError("Acknowledged successor is no longer running; original stays open")
            successor_key = next((o for o in self.core.store.data["observations"].values()
                                  if o["pane"] == heir["successor_pane"] and o["active"]), None)
            if not successor_key or successor_key["session"] != heir["successor_session"]:
                raise ValueError("Successor switched sessions after acknowledgment")
            self.core.herdr.verify_process(heir["successor_pane"], successor_key["pids"])
            # No foreground-process kills: Herdr closes exactly the original pane.
            self.core.herdr.close(source["pane"])
            self.save(handoff, state="retired", retired_at=self.core.clock())
            return {"retired": True, "id": handoff["id"]}

    def live_heir(self, handoff):
        # A successor that handed off in turn and retired passed the work on, so follow
        # the chain to the acknowledged successor still running. A crashed one ends it.
        handoffs = self.core.store.data["handoffs"].values()
        seen = set()
        while handoff["id"] not in seen:
            seen.add(handoff["id"])
            successor = self.core.agents.get(handoff["successor_pane"])
            if (successor and successor["terminal_id"] == handoff["successor_terminal"]
                    and successor.get("name") == handoff["successor_name"]):
                return handoff
            handoff = next((h for h in handoffs if h["state"] == "retired"
                            and h["source_pane"] == handoff["successor_pane"]
                            and h["source_session"] == handoff["successor_session"]), None)
            if not handoff:
                return None
        return None

    def cancel(self, request):
        with self.core.lock:
            handoff = self.get(request["id"])
            source = self.core.source(request)
            if source["key"] != handoff["source_key"]:
                raise ValueError("Only the original session may cancel")
            if handoff["state"] in ("acknowledged", "retired", "starting", "preparing"):
                raise ValueError("Cannot cancel an acknowledged or in-progress startup")
            # Require the user/agent to inspect and close the abandoned pane first.
            # Do not guess whether a failed prompt started work.
            panes = self.core.herdr.run("pane", "list")["panes"]
            if any(p["pane_id"] == handoff.get("successor_pane") for p in panes):
                raise ValueError("Inspect and close the abandoned successor pane through Herdr before cancelling")
            self.save(handoff, state="cancelled")
            return handoff
