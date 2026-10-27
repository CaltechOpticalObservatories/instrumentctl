"""``dashboard``: a web page showing this host's daemons, with start, stop and restart.

The three verbs go through systemctl just as they do from the CLI, so polkit
decides: it lets the ops group, and with it the daemon account the service
runs as, act on ``<name>@`` units and nothing else. There is no login.

The page asks for ``/api/status`` every couple of seconds. One ``systemctl
show`` answers for every instance and is shared between viewers for a second,
so ten open tabs cost no more than one.

Flask and waitress are the ``dashboard`` extra and imported only here, so the
rest of the CLI keeps working on a host without them.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Tuple

from . import hotfix
from . import instances as inst
from . import units
from .installer import _render
from .instances import Paths, TargetError

VERBS = ("start", "stop", "restart")
_DONE = {"start": "started", "stop": "stopped", "restart": "restarted"}

# How long one answer is shared. The checkout changes far less often than the
# daemons, and asking git costs several processes.
STATUS_TTL = 1.0
CHECKOUT_TTL = 30.0


class Cached:
    """A value recomputed at most every ``ttl`` seconds, however many ask."""

    def __init__(self, ttl: float, compute: Callable[[], Any]) -> None:
        self.ttl = ttl
        self.compute = compute
        self.lock = threading.Lock()
        self.value: Any = None
        self.taken = float("-inf")

    def get(self) -> Any:
        """The value, recomputed first if it is older than ``ttl``."""
        with self.lock:
            if time.monotonic() - self.taken >= self.ttl:
                self.value = self.compute()
                self.taken = time.monotonic()
            return self.value

    def clear(self) -> None:
        """Make the next ``get`` recompute, e.g. after starting something."""
        with self.lock:
            self.taken = float("-inf")


def _config_state(paths: Paths, name: str) -> str:
    """repo, differs, or missing: is the deployed config the committed one?"""
    config = paths.config(name)
    if not config.is_file():
        return "missing"
    found = inst.repo_configs(paths, name)
    if len(found) == 1 and config.read_bytes() != found[0].read_bytes():
        return "differs"
    return "repo"


def _daemon(paths: Paths, name: str) -> str:
    try:
        env = inst.read_env(paths.instances / f"{name}.env")
    except OSError:
        return "?"
    return env.get(paths.product.env_key("DAEMON"), "?")


def _instance(paths: Paths, name: str, state: units.UnitState) -> Dict[str, Any]:
    return {
        "name": name,
        # What `start <subsystem>` matches, so a group's buttons act on the group
        "subsystem": name.split("_", 1)[0],
        "daemon": _daemon(paths, name),
        "config": _config_state(paths, name),
        "active": state.active,
        "sub": state.sub,
        "boot": state.enabled,
        "since": state.since if state.active != "inactive" else "",
        "restarts": state.restarts,
        "result": state.result,
        "exit_status": state.exit_status,
        "pid": state.pid,
        "memory": state.memory,
    }


def snapshot(paths: Paths) -> Dict[str, Any]:
    """Every deployed instance and its state, plus what belongs here but is not."""
    product = paths.product
    here = inst.deployed(paths)
    role = inst.host_role(paths)
    state = units.states(product, here)
    # Another host's instances are its business, not something missing here
    waiting = [name for name in inst.in_repo(paths) if name not in here
               and inst.assigned_host(product, paths.repo_instances / f"{name}.env")
               in (None, role)]
    return {
        "instrument": {"name": product.name, "display_name": product.display_name},
        "host": role,
        "taken": time.time(),
        "instances": [_instance(paths, name, state[name]) for name in here],
        "undeployed": waiting,
    }


def checkout(paths: Paths) -> Dict[str, Any]:
    """Which code the daemons run: the branch, and anything a hotfix left behind."""
    problem = hotfix.checkout_problem(paths)
    if problem:
        return {"problem": problem}
    branch = hotfix.current_branch(paths)
    found = hotfix.changes(paths)
    return {
        "branch": branch,
        "base": paths.product.git_base,
        "hotfix": hotfix.is_hotfix(paths, branch),
        "edited": len(found.edited) + len(found.new),
        "unsent": hotfix.unsent(paths, branch) if branch else 0,
    }


def act(paths: Paths, verb: str, targets: List[str],
        origin: str) -> Tuple[int, Dict[str, Any]]:
    """Apply a verb to instances or subsystems, as ``<name> <verb>`` would.

    Returns an HTTP status and the per-instance outcome.
    """
    product = paths.product
    try:
        names = inst.resolve(targets, inst.deployed(paths))
    except TargetError as exc:
        return 404, {"error": f"not deployed on this host: {exc}"}
    if verb == "stop":
        # As from the CLI: undo a start in the opposite order
        names.reverse()
    state = units.states(product, names)
    results = []
    for name in names:
        skip = units.needless(state[name], verb)
        if skip:
            results.append({"name": name, "ok": True, "detail": skip})
            continue
        # Fail rather than wait for a password nobody can type
        proc = units.run(["systemctl", "--no-ask-password", verb, product.unit(name)],
                         capture=True)
        ok = proc.returncode == 0
        detail = _DONE[verb] if ok else (proc.stderr.strip().splitlines() or ["failed"])[-1]
        results.append({"name": name, "ok": ok, "detail": detail})
        print(f"{origin} {verb} {name}: {detail}", flush=True)
    failed = any(not r["ok"] for r in results)
    return (502 if failed else 200), {"results": results}


def create_app(paths: Paths) -> Any:
    """The Flask application: the page, its status feed, and the three verbs."""
    # Deferred, so a host without the dashboard extra can still run the rest
    from flask import Flask, Response, jsonify, request  # pylint: disable=import-outside-toplevel

    app = Flask(paths.product.name)
    page = _render("dashboard.html", paths.product, paths)
    status = Cached(STATUS_TTL, lambda: snapshot(paths))
    code = Cached(CHECKOUT_TTL, lambda: checkout(paths))

    @app.get("/")
    def index() -> Response:
        return Response(page, mimetype="text/html")

    @app.get("/api/status")
    def api_status() -> Response:
        return jsonify({**status.get(), "checkout": code.get()})

    @app.post("/api/<verb>")
    def api_act(verb: str) -> Tuple[Response, int]:
        if verb not in VERBS:
            return jsonify({"error": f"unknown action: {verb}"}), 404
        # A page elsewhere can make a browser on this network post a form
        # here, but not a JSON body, which needs a CORS preflight this never
        # answers
        if not request.is_json:
            return jsonify({"error": "send the targets as JSON"}), 415
        body = request.get_json(silent=True)
        targets = body.get("targets") if isinstance(body, dict) else None
        if (not isinstance(targets, list) or not targets
                or not all(isinstance(t, str) for t in targets)):
            return jsonify({"error": 'expected {"targets": ["<instance or subsystem>", ...]}'}), 400
        http_status, body = act(paths, verb, targets, request.remote_addr or "?")
        status.clear()
        return jsonify(body), http_status

    return app


def dashboard(paths: Paths, args: argparse.Namespace) -> int:
    """Serve the dashboard until interrupted."""
    product = paths.product
    try:
        import waitress  # pylint: disable=import-outside-toplevel
        app = create_app(paths)
    except ImportError as exc:
        print(f"{product.name}: the dashboard needs Flask and waitress ({exc.name} is "
              f"missing); install instrumentctl[dashboard] in {paths.venv}",
              file=sys.stderr)
        return 1
    bind: str = args.bind or product.dashboard_bind
    port: int = args.port or product.dashboard_port
    print(f"{product.name} dashboard on http://{bind}:{port}/", flush=True)
    # Each open page polls; a few threads keep one slow systemctl from
    # stalling the rest
    waitress.serve(app, host=bind, port=port, threads=8, ident=product.name)
    return 0
