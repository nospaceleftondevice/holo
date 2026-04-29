"""macOS-only AppKit / Quartz helpers (process activation, synthetic clicks).

`pyobjc-framework-Cocoa` (which provides AppKit / NSRunningApplication)
is a transitive dependency of `pyobjc-framework-Quartz`, so we don't
need to add it to pyproject.toml separately. Imports are local to
each call so this module can still be imported on non-darwin
platforms for tests / coverage.
"""

from __future__ import annotations

import subprocess


def activate_pid(pid: int) -> bool:
    """Bring the application with the given PID to the foreground.

    On macOS Sonoma (14)+ Apple restricted cross-app activation:
    `NSRunningApplication.activateWithOptions_` from a non-foreground
    process is silently denied. We try the modern API first (in case
    the calling app *is* the foreground app, or we're on an older
    OS), and fall back to AppleScript via osascript — which is
    treated by the OS as a privileged scripting framework and is
    still allowed to activate other apps.

    Returns True on success, False if no running app with that PID
    was found.
    """
    if pid <= 0:
        return False

    from AppKit import (  # type: ignore[import-not-found]
        NSApplicationActivateAllWindows,
        NSApplicationActivateIgnoringOtherApps,
        NSRunningApplication,
        NSWorkspace,
    )

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False

    # Fast path: the modern API. May silently fail on Sonoma+ if we
    # don't have user activation, but it's the right thing to call
    # when it works.
    options = NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps
    app.activateWithOptions_(options)

    # Belt-and-suspenders: ask osascript to do the same. osascript is
    # one of the routes Apple still permits to activate other apps
    # without an existing user-activation token. We address by name
    # (the localized application name macOS exposes), not pid —
    # AppleScript can't take a pid.
    name = app.localizedName()
    if name:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{name}" to activate'],
                check=False,
                capture_output=True,
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # osascript should always be present on macOS, but if it
            # isn't, we've already made the AppKit call; nothing else
            # to do.
            pass

    # NSWorkspace's frontmostApplication is updated synchronously
    # after activation requests succeed. Returning the result of the
    # activation lets callers wait the right amount of time.
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return front is not None and front.processIdentifier() == pid


def click_at(x: float, y: float) -> None:
    """Synthesize a left-click at screen coordinates (x, y).

    Posts CGEvent mouse-down + mouse-up events directly to the HID tap
    so the visible mouse cursor doesn't move. Used to focus the
    popup's contenteditable body before sending Cmd+V — Chrome opens
    new popups with OS keyboard focus on the address bar, and JS
    `.focus()` cannot move OS focus out of browser chrome.

    Requires the same Accessibility permission as keystroke
    synthesis; without it the events are silently dropped.
    """
    from Quartz import (  # type: ignore[import-not-found]
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )

    point = (float(x), float(y))
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, up)
