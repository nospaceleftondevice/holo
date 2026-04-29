"""macOS-only AppKit helpers (process activation).

`pyobjc-framework-Cocoa` (which provides AppKit / NSRunningApplication)
is a transitive dependency of `pyobjc-framework-Quartz`, so we don't
need to add it to pyproject.toml separately. The import is local to
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
