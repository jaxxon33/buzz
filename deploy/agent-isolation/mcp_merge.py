#!/usr/bin/env python3
"""Merge several stdio MCP servers into one (claude-opus, 2026-10-27).

buzz-acp hands an agent a single MCP command; this lets that command expose
buzz-dev-mcp plus extra servers (e.g. the hive desktop).

usage: mcp-merge <config.json>
config: {"servers": [{"name": "dev", "command": "...", "args": [], "env": {}}, ...]}
The first server is primary: it answers initialize and every non-tool request.
Tool names must be unique across servers (the first one wins on a clash).
"""
import atexit, json, os, signal, subprocess, sys, threading

MAX_LINE = 32 * 1024 * 1024  # desktop image responses are bounded below this
MAX_PENDING = 64
closing = threading.Event()


def fatal():
    # Wake the main thread even when it is blocked waiting for client input.
    if not closing.is_set():
        os.kill(os.getpid(), signal.SIGTERM)


def lines(stream):
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            fatal()
            return
        yield line

cfg = json.load(open(sys.argv[1]))
out_lock = threading.Lock()


def emit(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


class Child:
    def __init__(self, spec):
        self.name = spec["name"]
        env = dict(os.environ, **spec.get("env", {}))
        self.p = subprocess.Popen([spec["command"], *spec.get("args", [])], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, env=env, text=True, bufsize=1,
                                  start_new_session=True)
        self.lock = threading.Lock()

    def send(self, msg):
        with self.lock:
            try:
                self.p.stdin.write(json.dumps(msg) + "\n")
                self.p.stdin.flush()
            except (BrokenPipeError, ValueError):
                fatal()


children = []


def cleanup():
    # Child EOF during shutdown must not interrupt the bounded reap/kill path.
    closing.set()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for child in children:
        try:
            os.killpg(child.p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.p.wait(timeout=2)


atexit.register(cleanup)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
signal.signal(signal.SIGINT, lambda *_: sys.exit(1))
for spec in cfg["servers"]:
    children.append(Child(spec))
primary = children[0]
seq = 0
seq_lock = threading.Lock()
pending = {}        # proxy id -> (child, callback(msg))
server_reqs = {}    # id sent to client -> (child, child's original id)
tool_owner = {}


def call(child, method, params, cb):
    global seq
    with seq_lock:
        if len(pending) >= MAX_PENDING:
            fatal()
            return
        seq += 1
        pid = f"m{seq}"
    pending[pid] = (child, cb)
    child.send({"jsonrpc": "2.0", "id": pid, "method": method, "params": params})


def gather(method, params, done):
    """Send to every child; done(list of (child, msg)) once all answered."""
    results, lock = {}, threading.Lock()

    def make(c):
        def cb(msg):
            with lock:
                results[c.name] = (c, msg)
                if len(results) == len(children):
                    done([results[x.name] for x in children])
        return cb
    for c in children:
        call(c, method, params, make(c))


def reader(child):
    global seq
    for line in lines(child.p.stdout):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "method" in msg and "id" in msg:          # server-initiated request
            with seq_lock:
                if len(server_reqs) >= MAX_PENDING:
                    fatal()
                    return
                seq += 1
                cid = f"s{seq}"
            server_reqs[cid] = (child, msg["id"])
            emit({**msg, "id": cid})
        elif "method" in msg:                         # notification
            emit(msg)
        elif msg.get("id") in pending:
            _, cb = pending.pop(msg["id"])
            cb(msg)
    # A dead secondary must not strand initialize/tools-list or silently
    # remove desktop control from the tool surface. The harness can restart.
    fatal()


for c in children:
    threading.Thread(target=reader, args=(c,), daemon=True).start()


def reply(cid, msg):
    out = {"jsonrpc": "2.0", "id": cid}
    if "error" in msg:
        out["error"] = msg["error"]
    else:
        out["result"] = msg.get("result")
    emit(out)


for line in lines(sys.stdin):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method is None:                                # response to a server-initiated request
        if mid in server_reqs:
            child, orig = server_reqs.pop(mid)
            child.send({**msg, "id": orig})
        continue
    if mid is None:                                   # client notification: everyone gets it
        for c in children:
            c.send(msg)
        continue
    params = msg.get("params", {})
    if method == "initialize":
        def init_done(rs, mid=mid):
            reply(mid, rs[0][1])
        gather(method, params, init_done)
    elif method == "tools/list":
        def list_done(rs, mid=mid):
            tools = []
            for c, r in rs:
                for t in (r.get("result") or {}).get("tools", []):
                    if t["name"] not in tool_owner or tool_owner[t["name"]] is c:
                        tool_owner[t["name"]] = c
                        tools.append(t)
            reply(mid, {"result": {"tools": tools}})
        gather(method, params, list_done)
    elif method == "tools/call":
        owner = tool_owner.get(params.get("name"), primary)
        call(owner, method, params, lambda r, mid=mid: reply(mid, r))
    else:
        call(primary, method, params, lambda r, mid=mid: reply(mid, r))
