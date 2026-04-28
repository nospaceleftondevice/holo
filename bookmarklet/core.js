// In-page agent — DOM wiring.
//
// This module ties framing.js (frame encoding), title.js (page → daemon
// channel), and dispatch.js (command execution) into the working
// bookmarklet payload. Loaded into the browser as the bookmarklet's
// only side effect: install() sets up the paste target, listeners,
// title observer, and emits a calibration beacon.
//
// Top-level code is intentionally side-effect-free so the module can
// be imported and unit-tested in Node without a DOM. All browser
// access is gated behind install().

import { decodeFrame, encodeFrame } from "./framing.js";
import { DispatchError, dispatch } from "./dispatch.js";
import { encodeFramedTitle, encodePlainMarker } from "./title.js";

const PANEL_ID = "__holo_paste_target__";
const HOLO_MARKER_TAIL_RE = /\s*\[holo:[^\]]+\]\s*$/;

/**
 * Strip a trailing holo marker (framed or plain) from a title string.
 * Used to capture the page's "natural" title before the bookmarklet
 * adds its own marker, and to re-capture if the page changes the
 * title at runtime.
 */
export function stripHoloMarker(title) {
  if (typeof title !== "string") return "";
  return title.replace(HOLO_MARKER_TAIL_RE, "").trim();
}

/**
 * Install the in-page agent. Idempotent — calling install() a second
 * time on the same window returns the existing state object.
 *
 * Returns the state object (panel, session, originalTitle, …) for
 * inspection / smoke testing.
 */
export function install() {
  if (window.__holo) return window.__holo;

  const state = {
    session: globalThis.crypto.randomUUID(),
    panel: null,
    originalTitle: stripHoloMarker(document.title),
    titleObserver: null,
    lastWrittenTitle: null,
  };

  state.panel = createPanel();
  document.body.appendChild(state.panel);
  state.panel.focus();

  state.panel.addEventListener("paste", (event) => onPaste(event, state));

  // Re-capture the user's title when the page itself updates it (SPA
  // route changes, dynamic <title> writes). MutationObserver fires for
  // both our writes and the page's writes; we distinguish via
  // lastWrittenTitle so we don't flap.
  const titleEl = document.querySelector("title");
  if (titleEl) {
    state.titleObserver = new MutationObserver(() => onTitleMutation(state));
    state.titleObserver.observe(titleEl, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  // Navigation sentinel — daemon stops typing into a page that's leaving.
  window.addEventListener("pagehide", () => {
    writePlainMarker(state, `bye:${state.session}`);
  });

  // Calibration beacon — the daemon's first signal that we're alive.
  writePlainMarker(state, `cal:${state.session}`);

  window.__holo = state;
  return state;
}

function createPanel() {
  let panel = document.getElementById(PANEL_ID);
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = PANEL_ID;
  panel.contentEditable = "true";
  panel.spellcheck = false;
  panel.setAttribute("aria-label", "holo paste target");
  panel.title = "holo: paste target";
  Object.assign(panel.style, {
    position: "fixed",
    top: "0",
    right: "0",
    width: "16px",
    height: "16px",
    background: "rgba(0, 200, 0, 0.55)",
    border: "1px solid rgba(0, 100, 0, 0.7)",
    zIndex: "2147483647",
    overflow: "hidden",
    outline: "none",
    cursor: "pointer",
    // Make any pasted text invisible — the framed JSON is operational
    // data, not user-readable content.
    fontSize: "1px",
    color: "transparent",
    caretColor: "transparent",
  });
  return panel;
}

function onPaste(event, state) {
  event.preventDefault();
  event.stopPropagation();

  const raw = event.clipboardData?.getData("text/plain") ?? "";
  // Defensively clear the contenteditable so a future user paste
  // doesn't see leftover bytes from our last command.
  state.panel.textContent = "";

  let frame;
  try {
    frame = decodeFrame(raw);
  } catch {
    writePlainMarker(state, "err:decode");
    state.panel.focus();
    return;
  }

  let cmd;
  try {
    cmd = JSON.parse(new TextDecoder().decode(frame.data));
  } catch {
    sendReply(state, frame, {
      error: { code: "bad_command", message: "command body is not valid JSON" },
    });
    state.panel.focus();
    return;
  }

  let result;
  try {
    result = dispatch(cmd);
  } catch (err) {
    if (err instanceof DispatchError) {
      result = { error: { code: err.code, message: err.message } };
    } else {
      result = { error: { code: "internal", message: String(err) } };
    }
  }

  sendReply(state, frame, result);
  // Refocus for the next paste cycle. The daemon will also re-click
  // before each subsequent paste, but keeping focus here covers the
  // common case where nothing else has stolen it.
  state.panel.focus();
}

function sendReply(state, originalFrame, result) {
  const data = new TextEncoder().encode(JSON.stringify(result));
  const replyJson = encodeFrame({
    session: originalFrame.session,
    type: "result",
    data,
    id: originalFrame.id,
  });
  writeFramedTitle(state, replyJson);
}

function writeFramedTitle(state, frameJson) {
  const title = encodeFramedTitle(frameJson, state.originalTitle);
  state.lastWrittenTitle = title;
  document.title = title;
}

function writePlainMarker(state, marker) {
  const title = encodePlainMarker(marker, state.originalTitle);
  state.lastWrittenTitle = title;
  document.title = title;
}

function onTitleMutation(state) {
  const current = document.title;
  // Our own writes — ignore. Comparing the full string is safe because
  // we record exactly what we wrote.
  if (current === state.lastWrittenTitle) return;
  // The page changed the title underneath us. Capture its new
  // "natural" form (without any leftover holo marker, just in case)
  // so the next response uses the up-to-date prefix. We don't attempt
  // to re-emit a holo marker here — the next command/result will do
  // that. The daemon polls and will see the marker reappear then.
  state.originalTitle = stripHoloMarker(current);
}
