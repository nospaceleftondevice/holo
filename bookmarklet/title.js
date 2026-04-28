// Title-channel codec for the page → daemon direction.
//
// The bookmarklet writes responses and beacons into `document.title`,
// which the daemon reads via OS window-title APIs. Two title formats:
//
//   `<originalTitle> [holo:1:<base64-frame-json>]`
//        "framed" form — carries an encodeFrame() payload.
//   `<originalTitle> [holo:<marker>]`
//        "plain" form — short status beacon (cal, bye, err:...).
//
// Markers live at the *end* of the title so that browser tab strips
// (which truncate from the right) keep the user's original title
// visible. Window-title OS APIs return the full string regardless.
// `<originalTitle>` is captured at install and never modified — the
// page is free to update its own title; we re-append on each write.

const FRAMED_RE = /\[holo:1:([A-Za-z0-9+/=]+)\]\s*$/;
const PLAIN_RE = /\[holo:([^\]]+)\]\s*$/;

export function encodeFramedTitle(frameJson, originalTitle = "") {
  if (typeof frameJson !== "string") {
    throw new TypeError("frameJson must be a string");
  }
  const encoded = btoa(frameJson);
  return originalTitle ? `${originalTitle} [holo:1:${encoded}]` : `[holo:1:${encoded}]`;
}

export function decodeFramedTitle(title) {
  if (typeof title !== "string") return null;
  const m = title.match(FRAMED_RE);
  if (!m) return null;
  try {
    return atob(m[1]);
  } catch {
    return null;
  }
}

export function encodePlainMarker(marker, originalTitle = "") {
  if (typeof marker !== "string") {
    throw new TypeError("marker must be a string");
  }
  if (marker.includes("]")) {
    throw new Error("marker must not contain ']'");
  }
  if (marker.startsWith("1:")) {
    // Reserved for the framed form; refuse to emit a plain marker that
    // would round-trip as framed.
    throw new Error("marker must not start with '1:'");
  }
  return originalTitle ? `${originalTitle} [holo:${marker}]` : `[holo:${marker}]`;
}

export function decodePlainMarker(title) {
  if (typeof title !== "string") return null;
  const m = title.match(PLAIN_RE);
  if (!m) return null;
  // Refuse to interpret a framed payload as a plain marker.
  if (m[1].startsWith("1:")) return null;
  return m[1];
}

export function isHoloTitle(title) {
  return decodeFramedTitle(title) !== null || decodePlainMarker(title) !== null;
}
