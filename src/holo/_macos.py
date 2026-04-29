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


def keystroke_paste(app_name: str | None = None) -> bool:
    """Send Cmd+V via osascript + System Events.

    pyautogui's `hotkey('command', 'v')` works for keystrokes destined
    for the terminal (proven in early demo runs), but the resulting
    paste event has been observed to never reach a Chrome popup's
    contenteditable. System Events / osascript keystrokes share the
    same Automation pipeline that we're already using for activation,
    and have been observed to land where the page's paste handler can
    see them.

    If `app_name` is given, the script activates that app first so
    the keystroke targets it. Otherwise the keystroke goes to the
    current frontmost app.

    Returns True on success, False if osascript fails or isn't
    available.
    """
    if app_name:
        script = (
            f'tell application "{app_name}" to activate\n'
            'delay 0.2\n'
            'tell application "System Events" to keystroke "v" using command down\n'
        )
    else:
        script = 'tell application "System Events" to keystroke "v" using command down\n'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5.0,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def capture_window_qr(window_id: int) -> str | None:
    """Capture the given window's pixels and decode any QR code present.

    The popup renders its replies as QR codes on a canvas because the
    title channel is OS-truncated for any payload longer than ~70
    characters. Pixel capture has no such limit and is CSP-immune,
    so it works as the universal page → daemon channel.

    Returns the QR's payload string, or None if:
    - the window can't be captured (closed, off-screen, no permission)
    - no QR is detected in the captured image
    - more than one QR is detected (we expect exactly one)

    Uses the macOS Vision framework, which ships with the OS — no
    third-party native dependency.
    """
    from Quartz import (  # type: ignore[import-not-found]
        CGWindowListCreateImage,
        kCGNullWindowID,  # noqa: F401  (kept for type-stub completeness)
        kCGWindowImageBoundsIgnoreFraming,
        kCGWindowImageDefault,
        kCGWindowListOptionIncludingWindow,
    )

    image_ref = CGWindowListCreateImage(
        ((0, 0), (0, 0)),  # CGRectNull — capture the whole window
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageDefault | kCGWindowImageBoundsIgnoreFraming,
    )
    if image_ref is None:
        return None

    from Vision import (  # type: ignore[import-not-found]
        VNDetectBarcodesRequest,
        VNImageRequestHandler,
    )

    request = VNDetectBarcodesRequest.alloc().init()
    request.setSymbologies_(["VNBarcodeSymbologyQR"])

    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(image_ref, None)
    success, _err = handler.performRequests_error_([request], None)
    if not success:
        return None

    results = request.results() or []
    if not results:
        return None

    # We expect exactly one QR per popup at any given time. If the
    # popup is mid-render we may briefly see zero or two; caller
    # retries on a poll, so being strict here is safe.
    payloads = [obs.payloadStringValue() for obs in results]
    payloads = [p for p in payloads if p]
    if len(payloads) != 1:
        return None
    return str(payloads[0])


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
