// Unit tests for core.js. The popup-window plumbing is exercised
// through a hand-rolled DOM stub — small enough that pulling in
// jsdom isn't worth the dev-dependency. End-to-end paste/reply
// behavior against a real browser is covered by the integration
// demo (`holo demo`).

import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import {
  buildPopupBody,
  installInPopup,
  stripHoloMarker,
} from "../core.js";

// ---------- DOM stub ----------

function stubElement(tagName) {
  const listeners = {};
  return {
    tagName,
    children: [],
    style: {},
    attributes: {},
    listeners,
    contentEditable: "false",
    spellcheck: true,
    textContent: "",
    value: "",
    setAttribute(k, v) {
      this.attributes[k] = v;
    },
    addEventListener(name, fn) {
      (listeners[name] ||= []).push(fn);
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    focus() {
      this._focused = true;
    },
    fireEvent(name, event = {}) {
      for (const fn of listeners[name] ?? []) fn(event);
    },
  };
}

function stubWindow() {
  const titleEl = stubElement("title");
  titleEl.textContent = "";
  const body = stubElement("body");
  const doc = {
    body,
    _titleEl: titleEl,
    get title() {
      return titleEl.textContent;
    },
    set title(v) {
      titleEl.textContent = v;
      titleEl.fireEvent("childList");
    },
    querySelector(sel) {
      if (sel === "title") return titleEl;
      return null;
    },
    createElement(tag) {
      return stubElement(tag);
    },
  };
  const win = {
    document: doc,
    listeners: {},
    addEventListener(name, fn) {
      (this.listeners[name] ||= []).push(fn);
    },
    setTimeout: (fn) => fn(),
    MutationObserver: class {
      constructor(cb) {
        this._cb = cb;
      }
      observe(target) {
        target.addEventListener("childList", () => this._cb());
      }
    },
  };
  return win;
}

// ---------- pure helpers ----------

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
    assert.equal(
      stripHoloMarker("  Page Title   [holo:cal:1]  "),
      "Page Title",
    );
  });

  it("returns empty string for non-string input", () => {
    assert.equal(stripHoloMarker(null), "");
    assert.equal(stripHoloMarker(undefined), "");
    assert.equal(stripHoloMarker(42), "");
  });

  it("returns empty string when the title is just a marker", () => {
    assert.equal(stripHoloMarker("[holo:cal:1]"), "");
  });

  it("only strips a trailing marker, not embedded brackets earlier", () => {
    assert.equal(
      stripHoloMarker("[holo:something] in middle of title"),
      "[holo:something] in middle of title",
    );
  });
});

describe("core.js module exports", () => {
  it("exports install, installInPopup, buildPopupBody, stripHoloMarker", async () => {
    const mod = await import("../core.js");
    assert.equal(typeof mod.install, "function");
    assert.equal(typeof mod.installInPopup, "function");
    assert.equal(typeof mod.buildPopupBody, "function");
    assert.equal(typeof mod.stripHoloMarker, "function");
  });
});

// ---------- buildPopupBody ----------

describe("buildPopupBody", () => {
  it("creates a textarea paste target inside the body", () => {
    const win = stubWindow();
    const target = buildPopupBody(win.document);
    assert.equal(target.tagName, "textarea");
    assert.equal(win.document.body.children[0], target);
    assert.equal(target.spellcheck, false);
    assert.equal(target.attributes["aria-label"], "holo paste target");
    assert.equal(target.attributes["id"] ?? target.id, "__holo_paste_target__");
  });

  it("sets the popup document title", () => {
    const win = stubWindow();
    buildPopupBody(win.document);
    assert.equal(win.document.title, "holo console");
  });

  it("seeds the textarea with a 'keep this window open' note", () => {
    const win = stubWindow();
    const target = buildPopupBody(win.document);
    assert.match(target.value, /keep this window open/);
  });
});

// ---------- installInPopup ----------

describe("installInPopup", () => {
  it("emits a calibration beacon as a plain marker on the popup title", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    const sid = "abc-123";
    installInPopup(popup, opener, sid);
    assert.match(popup.document.title, /\[holo:cal:abc-123\]$/);
  });

  it("emits a bye marker on pagehide", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid-1");
    popup.listeners.pagehide?.[0]?.();
    assert.match(popup.document.title, /\[holo:bye:sid-1\]$/);
  });

  it("registers a paste listener on the textarea", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid");
    const textarea = popup.document.body.children[0];
    assert.ok(textarea.listeners.paste?.length >= 1);
  });

  it("focuses the textarea on install", () => {
    const popup = stubWindow();
    const opener = stubWindow();
    installInPopup(popup, opener, "sid");
    const textarea = popup.document.body.children[0];
    assert.equal(textarea._focused, true);
  });
});
