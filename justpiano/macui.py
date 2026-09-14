"""Thin wrappers around the native macOS bits we need (save panel, alerts,
notifications, Finder integration, login item)."""

from __future__ import annotations

import os
import subprocess
from typing import Optional


def _osascript(script: str, timeout: float = 60.0) -> Optional[str]:
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def _as_applescript(text: str) -> str:
    """Escape a Python string for use inside an AppleScript string literal.
    Backslashes first, then quotes, then the newlines a literal cannot hold."""
    return (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r"))


def save_panel(default_name: str, directory: str, file_type: str = "mid",
               prompt: str = "Save") -> Optional[str]:
    """Native save dialog. Returns the chosen path, or None if cancelled."""
    try:
        from AppKit import NSApplication, NSSavePanel
        from Foundation import NSURL

        panel = NSSavePanel.savePanel()
        panel.setTitle_(prompt)
        panel.setPrompt_("Save")
        panel.setNameFieldStringValue_(default_name)
        panel.setCanCreateDirectories_(True)
        try:
            import UniformTypeIdentifiers as UTI
            ut = UTI.UTType.typeWithFilenameExtension_(file_type)
            if ut is not None:
                panel.setAllowedContentTypes_([ut])
        except Exception:
            try:
                panel.setAllowedFileTypes_([file_type])
            except Exception:
                pass
        if directory and os.path.isdir(directory):
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(directory))
        try:
            # We are an accessory (menu bar) app, so the panel needs an explicit
            # nudge to come to the front. Note: pyobjc's AppKit.NSApp is a
            # *function*, not the application object.
            app = NSApplication.sharedApplication()
            if hasattr(app, "activate"):        # macOS 14+
                app.activate()
            else:
                app.activateIgnoringOtherApps_(True)
        except Exception:
            pass
        # NSModalResponseOK == 1
        return str(panel.URL().path()) if int(panel.runModal()) == 1 else None
    except Exception:
        pass

    # AppKit unavailable (e.g. running headless) -> AppleScript fallback.
    # `POSIX file ... as alias` throws if the folder is missing, so make sure it
    # exists and skip the coercion entirely.
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        directory = os.path.expanduser("~")
    safe_dir = _as_applescript(directory)
    safe_name = _as_applescript(default_name)
    script = (
        f'set f to choose file name with prompt "{_as_applescript(prompt)}" '
        f'default name "{safe_name}" '
        f'default location (POSIX file "{safe_dir}")\n'
        "POSIX path of f"
    )
    return _osascript(script, timeout=300)


def notify(title: str, message: str, subtitle: str = "") -> None:
    try:
        import rumps
        rumps.notification(title, subtitle, message)
        return
    except Exception:
        pass
    body = _as_applescript(message)
    head = _as_applescript(title)
    sub = _as_applescript(subtitle)
    _osascript(f'display notification "{body}" with title "{head}" subtitle "{sub}"',
               timeout=10)


def alert(title: str, message: str, ok: str = "OK") -> None:
    try:
        import rumps
        rumps.alert(title=title, message=message, ok=ok)
    except Exception:
        _osascript(
            f'display dialog "{_as_applescript(message)}" '
            f'with title "{_as_applescript(title)}" '
            f'buttons {{"{_as_applescript(ok)}"}} default button 1', timeout=300)


def reveal_in_finder(path: str) -> None:
    try:
        subprocess.run(["open", "-R", path], check=False)
    except Exception:
        pass


def open_path(path: str) -> None:
    try:
        subprocess.run(["open", path], check=False)
    except Exception:
        pass


def open_url(url: str) -> None:
    """Hand a URL to the system handler (e.g. a System Settings pane).

    `open` treats a scheme exactly as it treats a path, so this is `open_path`
    with a name that does not make its callers pretend a URL is a file.
    """
    open_path(url)


def bundle_path() -> Optional[str]:
    """Path of the enclosing .app bundle, or None when running from source."""
    try:
        from Foundation import NSBundle
        path = NSBundle.mainBundle().bundlePath()
        if path and str(path).endswith(".app"):
            return str(path)
    except Exception:
        pass
    return None


def is_login_item(app_path: str) -> Optional[bool]:
    """True/False when System Events answered, None when the query itself
    failed (Automation denied, osascript missing, timeout) - the caller must
    not read that as "not registered". Asking for this triggers the Automation
    prompt, so only call it after a user action."""
    out = _osascript(
        'tell application "System Events" to count (every login item whose '
        f'path is "{_as_applescript(app_path)}")', timeout=15)
    # Match on the bundle path the caller handed us: the display name is a
    # guess (an older build may have registered another one) and comparing
    # names cannot tell "absent" from "listed under a different name".
    if out is None:
        return None
    try:
        return int(out.strip()) > 0
    except ValueError:
        return None


def set_login_item(app_path: str, enabled: bool) -> bool:
    safe = _as_applescript(app_path)
    if enabled:
        name = os.path.splitext(os.path.basename(app_path))[0]
        script = (
            f'tell application "System Events" to make login item at end '
            f'with properties {{path:"{safe}", hidden:true, '
            f'name:"{_as_applescript(name)}"}}'
        )
    else:
        # Deleting a login item that is not there is an error in System Events,
        # which the caller would surface as a permissions failure. Requesting
        # the state we are already in must succeed - and `every ... whose path`
        # deletes an entry registered under any name, then quietly does nothing
        # once the list is empty.
        script = (
            f'tell application "System Events" to delete (every login item '
            f'whose path is "{safe}")'
        )
    return _osascript(script, timeout=20) is not None
