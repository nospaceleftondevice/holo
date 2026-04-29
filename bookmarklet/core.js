// In-page agent — opens a popup window that hosts the paste target and
// title channel.
//
// Why a popup: focus inside a host page like tai.sh competes with
// xterm.js, chat inputs, and the page's own focus management. A
// dedicated `about:blank` popup has nothing else to steal keyboard
// focus from our paste target, gets its own OS-level window title (no
// fighting with the host over `document.title`), and keeps the
// bookmarklet's moving parts out of the host page entirely.
//
// `about:blank` popups are same-origin with their opener for cross-
// document scripting, so dispatch can run against `window.opener`
// directly — `read_global("R2D2_VERSION")` resolves on the host page,
// not in the (empty) popup.
//
// Top-level code stays side-effect-free so the module can be imported
// from Node tests without a DOM. Browser access is gated behind
// install() / installInPopup().

import { decodeFrame, encodeFrame } from "./framing.js";
import { DispatchError, dispatch } from "./dispatch.js";
import { encodeFramedTitle, encodePlainMarker } from "./title.js";

const POPUP_NAME = "holo_console";
const POPUP_FEATURES = "popup=yes,width=320,height=160,resizable=yes";
const POPUP_TITLE = "holo console";
const READY_TEXT = "holo console — keep this window open";
const HOLO_MARKER_TAIL_RE = /\s*\[holo:[^\]]+\]\s*$/;

/**
 * Strip a trailing holo marker (framed or plain) from a title string.
 * Used to capture a window's "natural" title so the next response
 * keeps that prefix.
 */
export function stripHoloMarker(title) {
  if (typeof title !== "string") return "";
  return title.replace(HOLO_MARKER_TAIL_RE, "").trim();
}

/**
 * Install the in-page agent. Opens a popup window from the host page
 * and installs the paste target + title channel inside it.
 *
 * Idempotent: a second click focuses the existing popup if it's still
 * open, or opens a fresh one if the user closed it.
 */
export function install() {
  const existing = window.__holo;
  if (existing && existing.popup && !existing.popup.closed) {
    existing.popup.focus();
    return existing;
  }

  const session = globalThis.crypto.randomUUID();
  const popup = window.open("about:blank", POPUP_NAME, POPUP_FEATURES);
  if (!popup) {
    // Popup blocked — surface it via the host page's title so the
    // daemon sees a clear error instead of waiting for a calibration
    // beacon that will never arrive.
    document.title = encodePlainMarker("err:popup-blocked", document.title);
    throw new Error("holo: popup blocked — allow popups for this site");
  }

  installInPopup(popup, window, session);

  const state = { session, popup };
  window.__holo = state;
  return state;
}

/**
 * Wire up the paste target and title channel inside `popupWindow`.
 *
 * Exported separately so tests can drive it with stub window/document
 * objects; production callers go through install().
 */
export function installInPopup(popupWindow, openerWindow, session) {
  const popupDoc = popupWindow.document;
  const panel = buildPopupBody(popupDoc);

  const state = {
    session,
    popupWindow,
    openerWindow,
    panel,
    originalTitle: POPUP_TITLE,
    titleObserver: null,
    lastWrittenTitle: null,
  };

  const log = (...args) => popupWindow.console?.log?.("[holo]", ...args);
  log("install start", { session, sessionShort: session.slice(0, 8) });

  panel.addEventListener("paste", (event) => {
    log("paste event", {
      isTrusted: event.isTrusted,
      target: event.target?.tagName,
      activeElement: popupDoc.activeElement?.tagName,
      hasClipboardData: !!event.clipboardData,
      textLen: event.clipboardData?.getData("text/plain")?.length ?? 0,
    });
    onPaste(event, state);
  });

  // Catch ALL keystrokes the popup receives — even ones that don't
  // generate paste events. Diagnostic: if synthetic Cmd+V never even
  // produces a keydown, the keystroke isn't reaching the popup at the
  // OS level. If keydown fires but paste doesn't, Chrome is dropping
  // the paste path specifically.
  panel.addEventListener("keydown", (event) => {
    log("keydown", {
      key: event.key,
      code: event.code,
      metaKey: event.metaKey,
      ctrlKey: event.ctrlKey,
      isTrusted: event.isTrusted,
      target: event.target?.tagName,
    });
  });

  // Keep focus pinned on the panel. Anything focusing the window or
  // blurring the panel snaps back so the next Cmd+V always lands here.
  popupWindow.addEventListener("focus", () => {
    log("window focus");
    panel.focus();
  });
  popupWindow.addEventListener("blur", () => log("window blur"));
  panel.addEventListener("focus", () => log("panel focus"));
  panel.addEventListener("blur", () => {
    log("panel blur");
    popupWindow.setTimeout(() => panel.focus(), 0);
  });
  panel.focus();
  log("panel focused, ready", { activeElement: popupDoc.activeElement?.tagName });

  const titleEl = popupDoc.querySelector("title");
  if (titleEl && popupWindow.MutationObserver) {
    state.titleObserver = new popupWindow.MutationObserver(() =>
      onTitleMutation(state),
    );
    state.titleObserver.observe(titleEl, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  // pagehide fires when the popup is closed or navigates away —
  // give the daemon a clear signal so it doesn't keep typing into a
  // dead window.
  popupWindow.addEventListener("pagehide", () => {
    writePlainMarker(state, `bye:${session}`);
  });

  // Calibration beacon — daemon's first signal that we're alive.
  writePlainMarker(state, `cal:${session}`);

  return state;
}

/**
 * Build the popup's paste target — a <textarea> filling the body.
 * Returns the textarea so callers can attach listeners.
 *
 * Why a textarea (not body[contenteditable="true"]): Chromium routes
 * synthetic Cmd+V keystrokes (from osascript / pyautogui / CGEvent)
 * through a slightly different paste path than user-generated keys.
 * Real textareas pick up both reliably; contenteditable on body has
 * been observed to drop the synthetic ones, breaking the daemon →
 * page channel.
 */
export function buildPopupBody(popupDoc) {
  popupDoc.title = POPUP_TITLE;
  const body = popupDoc.body;
  Object.assign(body.style, {
    margin: "0",
    padding: "0",
    background: "rgb(15, 30, 15)",
    overflow: "hidden",
  });

  const textarea = popupDoc.createElement("textarea");
  textarea.id = "__holo_paste_target__";
  textarea.spellcheck = false;
  textarea.setAttribute("aria-label", "holo paste target");
  textarea.value = READY_TEXT;
  Object.assign(textarea.style, {
    display: "block",
    width: "100%",
    height: "100vh",
    margin: "0",
    padding: "12px",
    border: "none",
    background: "rgb(15, 30, 15)",
    color: "rgb(120, 200, 120)",
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
    fontSize: "12px",
    lineHeight: "1.4",
    boxSizing: "border-box",
    outline: "none",
    resize: "none",
    caretColor: "transparent",
  });
  body.appendChild(textarea);
  return textarea;
}

function onPaste(event, state) {
  const log = (...args) => state.popupWindow.console?.log?.("[holo]", ...args);
  event.preventDefault();
  event.stopPropagation();

  const raw = event.clipboardData?.getData("text/plain") ?? "";
  log("onPaste raw", { len: raw.length, preview: raw.slice(0, 60) });
  // Defensively reset the textarea value so a future user paste
  // doesn't see leftover bytes from our last command.
  state.panel.value = READY_TEXT;

  let frame;
  try {
    frame = decodeFrame(raw);
    log("decodeFrame ok", { id: frame.id, type: frame.type, session: frame.session?.slice(0, 8) });
  } catch (err) {
    log("decodeFrame failed", String(err));
    writePlainMarker(state, "err:decode");
    state.panel.focus();
    return;
  }

  let cmd;
  try {
    cmd = JSON.parse(new TextDecoder().decode(frame.data));
    log("cmd parsed", cmd);
  } catch {
    log("cmd parse failed");
    sendReply(state, frame, {
      error: { code: "bad_command", message: "command body is not valid JSON" },
    });
    state.panel.focus();
    return;
  }

  // Dispatch resolves read_global / read_dom / etc. against the
  // *opener* (host page), not the popup's own globals.
  const env = { window: state.openerWindow };
  let result;
  try {
    result = dispatch(cmd, env);
    log("dispatch ok", result);
  } catch (err) {
    if (err instanceof DispatchError) {
      result = { error: { code: err.code, message: err.message } };
    } else {
      result = { error: { code: "internal", message: String(err) } };
    }
    log("dispatch err", result);
  }

  sendReply(state, frame, result);
  log("reply sent, title is now", state.popupWindow.document.title.slice(0, 80));
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
  state.popupWindow.document.title = title;
}

function writePlainMarker(state, marker) {
  const title = encodePlainMarker(marker, state.originalTitle);
  state.lastWrittenTitle = title;
  state.popupWindow.document.title = title;
}

function onTitleMutation(state) {
  const current = state.popupWindow.document.title;
  if (current === state.lastWrittenTitle) return;
  // Nothing else is supposed to write the popup's title, but if
  // something does we capture the new prefix and roll with it.
  state.originalTitle = stripHoloMarker(current);
}
