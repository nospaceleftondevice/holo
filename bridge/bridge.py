# Jython bridge for holo.
#
# Runs inside SikuliX's bundled Jython 2.7 via:
#
#     java -jar sikulixapi.jar -r bridge.py -- [bridge args]
#
# Bridge args (after the `--` SikuliX uses to separate its own args from
# script args):
#
#     --transport stdio                       (default; daemon spawns JVM as child)
#     --transport tcp --bind 127.0.0.1 --port 7081 [--token T]
#
# Reads line-delimited JSON-RPC requests from the chosen transport,
# dispatches them to SikuliX APIs, writes JSON-RPC responses back. The
# protocol is transport-agnostic: dispatch() takes a parsed request dict
# and returns a parsed response dict; transports just frame and unframe.
#
# Important: this file runs under Jython 2.7. No f-strings, no type
# hints, no `from __future__ import annotations`, no walrus operator.
# Keep it boring.

import json
import sys
import traceback

# SikuliX classes & helpers. Guarded so the file can be syntax-checked
# by Python 3 tools without SikuliX on the classpath.
try:
    import sikuli  # noqa: F401  (provides App, Key, KeyModifier, Location, click, type, ...)
    from sikuli import App, Key, KeyModifier, Location
    _SIKULI_OK = True
except ImportError:
    sikuli = None
    App = Key = KeyModifier = Location = None
    _SIKULI_OK = False


PROTOCOL_VERSION = "1"


# ---- handlers -----------------------------------------------------------
#
# Each handler takes a dict of params and returns a JSON-serialisable
# result dict. Errors are raised as ordinary exceptions; dispatch()
# converts them into JSON-RPC error envelopes.

def handle_ping(_params):
    return {
        "pong": True,
        "protocol": PROTOCOL_VERSION,
        "sikuli": _SIKULI_OK,
    }


def handle_app_activate(params):
    name = params["name"]
    app = App(name)
    app.focus()
    return {"focused": True, "name": name}


def handle_screen_click(params):
    x = int(params["x"])
    y = int(params["y"])
    modifiers = params.get("modifiers", []) or []
    location = Location(x, y)
    if modifiers:
        for m in modifiers:
            sikuli.keyDown(_resolve_modifier_key(m))
        try:
            sikuli.click(location)
        finally:
            for m in modifiers:
                sikuli.keyUp(_resolve_modifier_key(m))
    else:
        sikuli.click(location)
    return {"clicked": True, "x": x, "y": y}


def handle_screen_key(params):
    # combo: "cmd+v", "shift+enter", "tab"
    combo = params["combo"]
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    target = _resolve_key(parts[-1])
    modifiers = parts[:-1]
    if modifiers:
        mask = 0
        for m in modifiers:
            mask = mask | _resolve_modifier_mask(m)
        sikuli.type(target, mask)
    else:
        sikuli.type(target)
    return {"sent": combo}


def handle_screen_type(params):
    text = params["text"]
    sikuli.type(text)
    return {"typed_chars": len(text)}


HANDLERS = {
    "ping": handle_ping,
    "app.activate": handle_app_activate,
    "screen.click": handle_screen_click,
    "screen.key": handle_screen_key,
    "screen.type": handle_screen_type,
}


# ---- key/modifier resolution -------------------------------------------

def _resolve_key(name):
    # Sikuli's Key class has constants like Key.ENTER, Key.TAB, Key.F1.
    # For any name we don't recognise as a constant, return the raw string
    # so type("a") sends a literal character.
    upper = name.upper()
    if Key is not None and hasattr(Key, upper):
        return getattr(Key, upper)
    return name


def _resolve_modifier_key(name):
    # For keyDown/keyUp we need the Key constant, not the modifier mask.
    upper = name.upper()
    aliases = {"CMD": "META", "COMMAND": "META", "WIN": "META", "OPT": "ALT"}
    upper = aliases.get(upper, upper)
    return getattr(Key, upper)


def _resolve_modifier_mask(name):
    upper = name.upper()
    aliases = {"CMD": "META", "COMMAND": "META", "WIN": "META", "OPT": "ALT"}
    upper = aliases.get(upper, upper)
    return getattr(KeyModifier, upper)


# ---- dispatch ----------------------------------------------------------

def dispatch(request):
    rid = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    handler = HANDLERS.get(method)
    if handler is None:
        return {
            "id": rid,
            "error": {"code": -32601, "message": "method not found: " + str(method)},
        }
    try:
        result = handler(params)
        return {"id": rid, "result": result}
    except Exception as e:
        return {
            "id": rid,
            "error": {
                "code": -32603,
                "message": str(e),
                "trace": traceback.format_exc(),
            },
        }


def _process_line(line):
    line = line.strip()
    if not line:
        return None
    try:
        request = json.loads(line)
    except ValueError as e:
        return {
            "id": None,
            "error": {"code": -32700, "message": "parse error: " + str(e)},
        }
    return dispatch(request)


# ---- transports --------------------------------------------------------

def serve_stdio():
    while True:
        line = sys.stdin.readline()
        if not line:
            return  # EOF — daemon closed our stdin
        response = _process_line(line)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def serve_tcp(host, port, token):
    # Java's networking from Jython — keeps us off the OS-Python socket
    # module and consistent across platforms.
    from java.io import (
        BufferedReader,
        BufferedWriter,
        InputStreamReader,
        OutputStreamWriter,
    )
    from java.net import InetSocketAddress, ServerSocket

    server = ServerSocket()
    server.bind(InetSocketAddress(host, port))
    sys.stderr.write("[bridge] tcp listening on %s:%d\n" % (host, port))
    sys.stderr.flush()
    try:
        while True:
            client = server.accept()
            try:
                _serve_tcp_connection(client, token)
            finally:
                client.close()
    finally:
        server.close()


def _serve_tcp_connection(client, token):
    from java.io import (
        BufferedReader,
        BufferedWriter,
        InputStreamReader,
        OutputStreamWriter,
    )

    reader = BufferedReader(InputStreamReader(client.getInputStream(), "UTF-8"))
    writer = BufferedWriter(OutputStreamWriter(client.getOutputStream(), "UTF-8"))

    # Token handshake: first line must be {"token": "..."} when token is set.
    first = reader.readLine()
    if first is None:
        return
    try:
        hs = json.loads(first)
    except ValueError:
        hs = {}
    if token and hs.get("token") != token:
        writer.write(json.dumps({"ok": False, "error": "bad token"}) + "\n")
        writer.flush()
        return
    writer.write(json.dumps({"ok": True, "protocol": PROTOCOL_VERSION}) + "\n")
    writer.flush()

    while True:
        line = reader.readLine()
        if line is None:
            return
        response = _process_line(line + "\n")
        if response is None:
            continue
        writer.write(json.dumps(response) + "\n")
        writer.flush()


# ---- entry -------------------------------------------------------------

def parse_args(argv):
    args = {"transport": "stdio", "host": "127.0.0.1", "port": 0, "token": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--transport" and i + 1 < len(argv):
            args["transport"] = argv[i + 1]
            i += 2
        elif a in ("--bind", "--host") and i + 1 < len(argv):
            args["host"] = argv[i + 1]
            i += 2
        elif a == "--port" and i + 1 < len(argv):
            args["port"] = int(argv[i + 1])
            i += 2
        elif a == "--token" and i + 1 < len(argv):
            args["token"] = argv[i + 1]
            i += 2
        else:
            i += 1
    return args


def _script_args(argv):
    # SikuliX puts script args after a literal `--`. If the separator
    # isn't present, assume argv is already trimmed.
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return list(argv)


def main(argv):
    args = parse_args(_script_args(argv))
    if args["transport"] == "stdio":
        serve_stdio()
    elif args["transport"] == "tcp":
        if args["port"] == 0:
            sys.stderr.write("[bridge] tcp transport requires --port\n")
            sys.exit(2)
        serve_tcp(args["host"], args["port"], args["token"])
    else:
        sys.stderr.write("[bridge] unknown transport: " + str(args["transport"]) + "\n")
        sys.exit(2)


# SikuliX's `-r` runs the script with __name__ == "__main__", same as
# CPython, so the standard guard works. Kept explicit for clarity.
if __name__ == "__main__":
    main(sys.argv)
