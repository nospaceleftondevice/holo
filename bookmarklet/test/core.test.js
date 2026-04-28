// core.js touches the DOM in install(); we can't exercise that path
// in Node without jsdom. What we *can* verify here is that the module
// imports cleanly (no top-level browser API access), and that its
// pure-logic helper stripHoloMarker behaves correctly.
//
// End-to-end DOM behavior is verified manually and via the
// walking-skeleton integration test that lands with the daemon-side
// channel coordinator.

import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { stripHoloMarker } from "../core.js";

describe("stripHoloMarker", () => {
  it("removes a trailing framed marker", () => {
    assert.equal(stripHoloMarker("Page Title [holo:1:eyJ9]"), "Page Title");
  });

  it("removes a trailing plain marker", () => {
    assert.equal(stripHoloMarker("Page Title [holo:cal:abc]"), "Page Title");
  });

  it("returns titles without markers unchanged", () => {
    assert.equal(stripHoloMarker("Plain Page Title"), "Plain Page Title");
  });

  it("trims surrounding whitespace", () => {
    assert.equal(stripHoloMarker("  Page Title   [holo:cal:1]  "), "Page Title");
  });

  it("returns empty string for non-string input", () => {
    assert.equal(stripHoloMarker(null), "");
    assert.equal(stripHoloMarker(undefined), "");
    assert.equal(stripHoloMarker(42), "");
  });

  it("returns empty string when the title is just a marker", () => {
    assert.equal(stripHoloMarker("[holo:cal:1]"), "");
  });

  it("only strips the trailing marker, not embedded brackets earlier in the title", () => {
    // A title that happens to contain the substring "[holo:" earlier on
    // (unlikely in practice, but guard against false positives).
    assert.equal(stripHoloMarker("[holo:something] in middle of title"), "[holo:something] in middle of title");
  });
});

describe("core.js module import", () => {
  it("exports install and stripHoloMarker", async () => {
    const mod = await import("../core.js");
    assert.equal(typeof mod.install, "function");
    assert.equal(typeof mod.stripHoloMarker, "function");
  });
});
