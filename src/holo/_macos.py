"""macOS-only AppKit / Quartz helpers (process activation, synthetic clicks).

`pyobjc-framework-Cocoa` (which provides AppKit / NSRunningApplication)
is a transitive dependency of `pyobjc-framework-Quartz`, so we don't
need to add it to pyproject.toml separately. Imports are local to
each call so this module can still be imported on non-darwin
platforms for tests / coverage.
"""

from __future__ import annotations


def activate_pid(pid: int) -> bool:
    """Bring the application with the given PID to the foreground.

    Returns True if the activation request was made (the OS may still
    decline it under some conditions), False if no running app with
    that PID was found.

    Used before sending a synthesized Cmd+V so the keystroke lands in
    the holo console popup window rather than whatever else has
    focus (e.g. the terminal we're running from).
    """
    if pid <= 0:
        return False
    # AppKit lives in the Cocoa framework binding.
    from AppKit import (  # type: ignore[import-not-found]
        NSApplicationActivateAllWindows,
        NSApplicationActivateIgnoringOtherApps,
        NSRunningApplication,
    )

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False
    options = NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps
    app.activateWithOptions_(options)
    return True


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
