// Command dispatcher for the in-page agent.
//
// The bookmarklet decodes a frame whose data is a JSON command object
// like `{op: "ping"}` or `{op: "read_global", path: "R2D2_VERSION"}`
// and calls dispatch() to execute it, returning a JSON-serializable
// result object the channel layer can encode into a reply frame.
//
// Why no `eval`/`Function`: tai.sh and many other origins ship a
// strict CSP without `'unsafe-eval'`, so dynamic code execution from
// the bookmarklet is blocked. Instead, dispatch exposes a small set
// of structured operations (read_global, read_dom, click, …) that
// cover the agent surface without crossing the eval boundary.
//
// `env` lets callers inject `window`/`document` for testability;
// when omitted, the global ones are used in a browser context.

export class DispatchError extends Error {
  constructor(message, code = "dispatch_error") {
    super(message);
    this.name = "DispatchError";
    this.code = code;
  }
}

export function dispatch(cmd, env = {}) {
  if (cmd === null || typeof cmd !== "object") {
    throw new DispatchError("command must be an object", "bad_command");
  }
  const op = cmd.op;
  if (typeof op !== "string") {
    throw new DispatchError("command must have a string `op` field", "bad_command");
  }
  const handler = HANDLERS[op];
  if (!handler) {
    throw new DispatchError(`unknown op: ${op}`, "unknown_op");
  }
  return handler(cmd, env);
}

const HANDLERS = {
  ping(_cmd, _env) {
    return { pong: true };
  },

  read_global(cmd, env) {
    if (typeof cmd.path !== "string" || cmd.path === "") {
      throw new DispatchError("read_global requires a non-empty `path`", "bad_arg");
    }
    const root = env.window ?? globalThis.window ?? globalThis;
    return { value: readPath(root, cmd.path) };
  },
};

// Walks a dotted path against an object root. `cur?.[part]` would skip
// over present-but-falsy values (0, ""), so we explicitly check for
// null/undefined and otherwise continue indexing.
export function readPath(root, path) {
  const parts = path.split(".");
  let cur = root;
  for (const part of parts) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[part];
  }
  return cur;
}
