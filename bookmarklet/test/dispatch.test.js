import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { DispatchError, dispatch, readPath } from "../dispatch.js";

describe("dispatch", () => {
  it("rejects non-object commands", () => {
    assert.throws(() => dispatch(null), DispatchError);
    assert.throws(() => dispatch("ping"), DispatchError);
    assert.throws(() => dispatch(42), DispatchError);
  });

  it("rejects commands without a string `op`", () => {
    assert.throws(() => dispatch({}), { name: "DispatchError", code: "bad_command" });
    assert.throws(() => dispatch({ op: 5 }), { name: "DispatchError", code: "bad_command" });
  });

  it("rejects unknown ops", () => {
    assert.throws(
      () => dispatch({ op: "nope" }),
      { name: "DispatchError", code: "unknown_op" }
    );
  });
});

describe("dispatch: ping", () => {
  it("returns {pong: true}", () => {
    assert.deepEqual(dispatch({ op: "ping" }), { pong: true });
  });
});

describe("dispatch: read_global", () => {
  it("returns the value at a top-level path", () => {
    const env = { window: { R2D2_VERSION: "0.23" } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "R2D2_VERSION" }, env),
      { value: "0.23" }
    );
  });

  it("walks nested object paths", () => {
    const env = { window: { S3R9: { Config: { apiBase: "https://api-dev.tai.sh" } } } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "S3R9.Config.apiBase" }, env),
      { value: "https://api-dev.tai.sh" }
    );
  });

  it("returns undefined for missing paths", () => {
    const env = { window: { foo: 1 } };
    assert.deepEqual(
      dispatch({ op: "read_global", path: "bar.baz" }, env),
      { value: undefined }
    );
  });

  it("preserves falsy values", () => {
    const env = { window: { count: 0, empty: "", flag: false } };
    assert.deepEqual(dispatch({ op: "read_global", path: "count" }, env), { value: 0 });
    assert.deepEqual(dispatch({ op: "read_global", path: "empty" }, env), { value: "" });
    assert.deepEqual(dispatch({ op: "read_global", path: "flag" }, env), { value: false });
  });

  it("rejects empty or non-string path", () => {
    assert.throws(
      () => dispatch({ op: "read_global", path: "" }),
      { name: "DispatchError", code: "bad_arg" }
    );
    assert.throws(
      () => dispatch({ op: "read_global", path: 42 }),
      { name: "DispatchError", code: "bad_arg" }
    );
  });

  it("falls back to globalThis when env.window is omitted", () => {
    globalThis.__holoTestSentinel = "from-globalThis";
    try {
      const result = dispatch({ op: "read_global", path: "__holoTestSentinel" });
      assert.equal(result.value, "from-globalThis");
    } finally {
      delete globalThis.__holoTestSentinel;
    }
  });
});

describe("readPath", () => {
  it("returns root when path is empty after split", () => {
    // path.split(".") on "" gives [""], which then misses on root.
    // This is intentional — empty path means "no field," not "root".
    assert.equal(readPath({ x: 1 }, ""), undefined);
  });

  it("stops at first null/undefined ancestor", () => {
    assert.equal(readPath({ a: null }, "a.b.c"), undefined);
    assert.equal(readPath({}, "a.b"), undefined);
  });

  it("preserves intermediate falsy values that are not nullish", () => {
    assert.equal(readPath({ a: { b: 0 } }, "a.b"), 0);
    assert.equal(readPath({ a: { b: "" } }, "a.b"), "");
  });
});
