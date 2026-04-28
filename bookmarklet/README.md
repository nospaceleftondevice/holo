# bookmarklet/

In-page agent for the holo browser-automation primitive.

This directory holds the JS that runs in the user's browser when they click
the holo bookmark on their bookmarks bar — the page side of the channel that
the Python daemon talks to.

## Layout

- `framing.js` — JS port of `holo.framing`. Same wire format as the Python
  side; frames produced here decode cleanly on the daemon side and vice
  versa. No external dependencies.
- `test/framing.test.js` — Node `node:test` suite. Run via `npm test`.

## Roadmap

Subsequent PRs will add:

- `core.js` — the bookmarklet entry point. Sets up the hidden contenteditable
  for paste reception, installs the `paste` listener, writes responses
  through `document.title`, and emits a calibration beacon on first run.
- `bundle.js` — built single-file payload for inlining into the
  `javascript:` bookmarklet URL.

## Notes

- Targets browsers and Node 20+ (uses `globalThis.crypto.randomUUID`,
  `atob`/`btoa`, `JSON`, `TextEncoder`).
- No build step required for tests — Node runs the source directly.
