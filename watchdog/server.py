import fcntl
import json
import os
import signal
import socket
import socketserver
import threading

from .core import Core
from .handoff import Handoffs
from .herdr import Herdr
from .store import Store


MAX_PACKET = 128 * 1024


def rpc(paths, request, timeout=5):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(paths.socket))
        except (FileNotFoundError, ConnectionRefusedError):
            raise RuntimeError(f"Watchdog is not running (no daemon at {paths.socket}).\nStart it with `acw start`.") from None
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(4 * 1024 * 1024))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


def serve(paths, command, herdr=None, stop_event=None):
    os.umask(0o077)
    stop = stop_event or threading.Event()
    with (paths.state / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Watchdog is already running for this Herdr session") from None
        store = Store(paths.database)
        core = Core(store, herdr or Herdr(), paths.config, command)
        handoffs = Handoffs(core, paths.state / "handoffs")
        # Interrupted startup is ambiguous: retain its reservation, never auto-retry.
        for handoff in store.data["handoffs"].values():
            if handoff["state"] in ("starting", "preparing"):
                handoff.update(state="failed", error="Watchdog restarted during startup; inspect the successor before retrying")
        store.save()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    packet = self.rfile.readline(MAX_PACKET + 1)
                    if len(packet) > MAX_PACKET:
                        raise ValueError("Request too large")
                    request = json.loads(packet)
                    operation = request.get("op")
                    if operation == "observe":
                        result = core.observe(request)
                    elif operation == "delivered":
                        result = core.delivered(request)
                    elif operation == "prepared":
                        result = core.prepared(request)
                    elif operation == "status":
                        result = core.status()
                    elif operation == "stop":
                        stop.set()
                        result = {"stopping": True}
                    elif operation == "handoff.start":
                        result = handoffs.start(request)
                    elif operation == "handoff.ready":
                        result = handoffs.ready(request)
                    elif operation == "handoff.retire":
                        result = handoffs.retire(request)
                    elif operation == "handoff.cancel":
                        result = handoffs.cancel(request)
                    elif operation == "handoff.show":
                        with core.lock:
                            result = dict(handoffs.get(request["id"]))
                    else:
                        raise ValueError("Unknown operation")
                    response = {"result": result}
                except Exception as exc:
                    response = {"error": str(exc)}
                try:
                    self.wfile.write(json.dumps(response).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            # Wait for handoff operations to finish before closing the database.
            daemon_threads = False

        paths.socket.parent.mkdir(parents=True, exist_ok=True)
        paths.socket.unlink(missing_ok=True)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())

        def prompt_retirements():
            # Herdr prompts are slow; keep them off the request loop that hooks wait on.
            while not stop.wait(1):
                try:
                    handoffs.prompt_retirements()
                except Exception as exc:
                    print(f"acw: retire prompt sweep failed: {exc}", flush=True)

        prompter = threading.Thread(target=prompt_retirements, name="acw-retire-prompts")
        prompter.start()
        try:
            with Server(str(paths.socket), Handler) as server:
                paths.socket.chmod(0o600)
                server.timeout = 0.5
                while not stop.is_set():
                    core.refresh()
                    server.handle_request()
        finally:
            stop.set()
            prompter.join()
            paths.socket.unlink(missing_ok=True)
            store.close()
