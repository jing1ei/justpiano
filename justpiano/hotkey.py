"""
The global mute hotkey: parsing, matching, and the two NSEvent monitors.

Everything above `HotkeyMonitor` is pure Python - a spec string becomes a (key
code, modifier mask) pair, and a key-down event either is that hotkey or is not -
so the interesting half is testable headless. AppKit is only touched inside the
monitor, lazily, and every entry point degrades instead of raising: the same
"available(), or nothing happens" contract `macui` and `keyboardview` follow.

**Why two monitors.** Without adding a dependency the only system-wide keyboard
hook available is `NSEvent.addGlobalMonitorForEventsMatchingMask:handler:`, and
that one needs Accessibility trust. So a *local* monitor is installed as well: it
needs no permission at all and covers the case where Just Piano itself is in front
(its keyboard panel, or its menu). Trust missing therefore costs the "works while
a DAW has focus" half of the feature, never the feature - and never a crash or a
prompt.

**Why the exact-modifier match.** A global monitor cannot swallow the event: the
focused app sees the key too, so a combination a DAW binds would fire in both
places. Nothing here can prevent that, which is why the presets the tray offers
are combinations no DAW is likely to use - and why a *superset* of a hotkey's
modifiers is treated as somebody else's shortcut rather than as ours.
"""

from __future__ import annotations

import traceback
from typing import Callable, NamedTuple, Optional

#: NSEventModifierFlag* values (AppKit), so the parser needs no AppKit at all.
SHIFT = 1 << 17
CONTROL = 1 << 18
OPTION = 1 << 19
COMMAND = 1 << 20
FUNCTION = 1 << 23

#: The flags a shortcut may depend on. Deliberately *not* the whole
#: NSEventModifierFlagDeviceIndependentFlagsMask: caps lock (1 << 16), the
#: numeric-pad bit (1 << 21) and NSEventModifierFlagFunction are all set by the
#: keyboard on its own - macOS reports fn for every F-key - so a hotkey compared
#: against the full mask would never match on the machines that set them.
MODIFIER_MASK = SHIFT | CONTROL | OPTION | COMMAND

#: NSEventMaskKeyDown: the masks are 1 << the NSEventType (keyDown = 10).
KEY_DOWN_MASK = 1 << 10

#: System Settings pane the user has to visit when the global half is refused.
ACCESSIBILITY_PANE = ("x-apple.systempreferences:"
                      "com.apple.preference.security?Privacy_Accessibility")

MODIFIER_TOKENS = {
    "cmd": COMMAND, "command": COMMAND, "⌘": COMMAND,
    "ctrl": CONTROL, "control": CONTROL, "⌃": CONTROL,
    "alt": OPTION, "opt": OPTION, "option": OPTION, "⌥": OPTION,
    "shift": SHIFT, "⇧": SHIFT,
}

#: Modifier symbols in the order macOS writes them, for the menu labels.
MODIFIER_SYMBOLS = ((CONTROL, "⌃"), (OPTION, "⌥"), (SHIFT, "⇧"), (COMMAND, "⌘"))

#: Virtual key codes (kVK_*). ANSI positions, so this is what the key *is*
#: rather than what it types: `⌥m` produces "µ", and matching on characters
#: would miss it.
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
    "k": 40, "n": 45, "m": 46,
    "space": 49, "escape": 53, "esc": 53, ",": 43, ".": 47, "/": 44, ";": 41,
    "'": 39, "[": 33, "]": 30, "-": 27, "=": 24, "`": 50,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105,
    "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "f20": 90,
}

#: Reverse map for the labels: one name per code, the first one listed.
_KEY_NAMES: dict[int, str] = {}
for _name, _code in KEY_CODES.items():
    _KEY_NAMES.setdefault(_code, _name)


class Hotkey(NamedTuple):
    """One parsed shortcut: which physical key, with which modifiers held."""

    spec: str
    key_code: int
    modifiers: int

    def matches(self, key_code: int, flags: int) -> bool:
        """True when a key-down event is this hotkey.

        Exact on the four modifiers rather than "at least these": a superset
        (⇧ added, say) belongs to whatever the user is really driving, and a
        global monitor firing on it would mute the piano in the middle of
        somebody else's shortcut.
        """
        try:
            return (int(key_code) == self.key_code
                    and (int(flags) & MODIFIER_MASK) == self.modifiers)
        except (TypeError, ValueError):
            return False

    @property
    def label(self) -> str:
        """How macOS would print it: "⌃⌥⌘M", "F13"."""
        symbols = "".join(sym for flag, sym in MODIFIER_SYMBOLS
                          if self.modifiers & flag)
        name = _KEY_NAMES.get(self.key_code, "?")
        return symbols + (name.upper() if len(name) <= 3 else name.capitalize())


def parse(spec) -> Optional[Hotkey]:
    """Turn "ctrl+alt+cmd+m" into a `Hotkey`; None means "no hotkey".

    Nonsense is not an error either: `settings.json` is a plain file a user can
    edit, and a typo in it has to cost the hotkey, not the launch. The `spec`
    stored on the result is normalised, so it is also what gets persisted.
    """
    if not isinstance(spec, str):
        return None
    parts = [part.strip().lower() for part in spec.split("+")]
    parts = [part for part in parts if part]
    if not parts:
        return None
    modifiers = 0
    for token in parts[:-1]:
        flag = MODIFIER_TOKENS.get(token)
        if flag is None:
            return None
        modifiers |= flag
    key_code = KEY_CODES.get(parts[-1])
    if key_code is None:
        return None
    names = [name for name, flag in (("ctrl", CONTROL), ("alt", OPTION),
                                     ("shift", SHIFT), ("cmd", COMMAND))
             if modifiers & flag]
    names.append(_KEY_NAMES.get(key_code, parts[-1]))
    return Hotkey("+".join(names), key_code, modifiers)


def label(spec) -> Optional[str]:
    """The printable form of a spec, or None when it is not a hotkey."""
    parsed = parse(spec)
    return None if parsed is None else parsed.label


# ------------------------------------------------------------------- AppKit
_NSEVENT = None
_NSEVENT_TRIED = False
_AX_TRUSTED = None
_AX_TRIED = False


def _nsevent():
    """AppKit's NSEvent class, or None when pyobjc is not there. Cached."""
    global _NSEVENT, _NSEVENT_TRIED
    if _NSEVENT_TRIED:
        return _NSEVENT
    _NSEVENT_TRIED = True
    try:
        from AppKit import NSEvent
    except Exception:
        return None
    _NSEVENT = NSEvent
    return _NSEVENT


def available() -> bool:
    """Whether event monitors can be installed here at all."""
    return _nsevent() is not None


def _ax_is_trusted():
    """Bind `AXIsProcessTrusted`, or None if it cannot be reached. Cached.

    ApplicationServices is not in `requirements.txt` (only
    pyobjc-framework-Cocoa is), so the import is tried first and the C function
    is otherwise bound straight out of the system framework with pyobjc-core -
    which Cocoa already depends on. `Z` is pyobjc's encoding for a Boolean
    return, and `AXIsProcessTrusted` takes no arguments.
    """
    global _AX_TRUSTED, _AX_TRIED
    if _AX_TRIED:
        return _AX_TRUSTED
    _AX_TRIED = True
    try:
        from ApplicationServices import AXIsProcessTrusted
        _AX_TRUSTED = AXIsProcessTrusted
        return _AX_TRUSTED
    except Exception:
        pass
    try:
        import objc
        from Foundation import NSBundle

        bundle = NSBundle.bundleWithPath_(
            "/System/Library/Frameworks/ApplicationServices.framework")
        if bundle is None:
            return None
        namespace: dict = {}
        objc.loadBundleFunctions(bundle, namespace, [("AXIsProcessTrusted", b"Z")])
        _AX_TRUSTED = namespace.get("AXIsProcessTrusted")
    except Exception:
        _AX_TRUSTED = None
    return _AX_TRUSTED


def trusted() -> Optional[bool]:
    """Is this process trusted for Accessibility? None = could not find out.

    "Unknown" must never be shown to the user as "denied": without pyobjc, or if
    the symbol cannot be bound, there is nothing to report and the local monitor
    still works. This only ever *queries* - `AXIsProcessTrustedWithOptions` with
    the prompt option is the call that nags, and it is deliberately not used.
    """
    func = _ax_is_trusted()
    if func is None:
        return None
    try:
        return bool(func())
    except Exception:
        return None


class HotkeyMonitor:
    """The key-down monitors behind the mute hotkey.

    `apply()` arms a spec (or `None` to disarm) and `stop()` tears the monitors
    down again - which the tray must do on quit: `addLocal/GlobalMonitor...`
    hand back a token AppKit retains until `removeMonitor:` is called, and a
    monitor left behind keeps calling into a half-shut-down app.

    Both handlers run on the main thread (they are dispatched by the run loop),
    which is what makes it safe for `on_trigger` to touch the menu and the icon
    directly.
    """

    def __init__(self, on_trigger: Callable[[], None]) -> None:
        self._on_trigger = on_trigger
        self._hotkey: Optional[Hotkey] = None
        self._tokens: list = []
        #: Which halves are up. `global_installed` False with a hotkey armed is
        #: exactly the "Accessibility not granted yet" state the menu explains.
        self.local_installed = False
        self.global_installed = False
        #: Bumped on every match, so the suites can prove a monitor fired.
        self.triggers = 0

    @property
    def hotkey(self) -> Optional[Hotkey]:
        """The armed hotkey, or None when there is none."""
        return self._hotkey

    @property
    def installed(self) -> bool:
        return bool(self._tokens)

    def apply(self, spec) -> Optional[Hotkey]:
        """Arm `spec`, replacing whatever was armed. Returns the new hotkey."""
        self.stop()
        self._hotkey = parse(spec)
        if self._hotkey is not None:
            self._install()
        return self._hotkey

    def stop(self) -> None:
        """Remove every monitor. Idempotent, and safe to call after failure."""
        tokens, self._tokens = self._tokens, []
        self.local_installed = self.global_installed = False
        nsevent = _nsevent()
        for token in tokens:
            if nsevent is None:
                break
            try:
                nsevent.removeMonitor_(token)
            except Exception:
                traceback.print_exc()

    # ----------------------------------------------------------- internals
    def _install(self) -> None:
        nsevent = _nsevent()
        if nsevent is None:
            return
        try:
            token = nsevent.addLocalMonitorForEventsMatchingMask_handler_(
                KEY_DOWN_MASK, self._local_handler)
        except Exception:
            traceback.print_exc()
            token = None
        if token is not None:
            self._tokens.append(token)
            self.local_installed = True
        if trusted() is False:
            # Not trusted: a global monitor is accepted and then simply never
            # called, so there is nothing to retain and no point logging one
            # attempt per launch. The tray offers the Settings pane instead, and
            # re-arms once trust appears.
            return
        try:
            token = nsevent.addGlobalMonitorForEventsMatchingMask_handler_(
                KEY_DOWN_MASK, self._global_handler)
        except Exception:
            traceback.print_exc()
            token = None
        if token is not None:
            self._tokens.append(token)
            self.global_installed = True

    def _local_handler(self, event):
        """Local monitors may swallow an event by returning None - this one
        never does: the key belongs to whatever is focused as much as to us."""
        self._handle(event)
        return event

    def _global_handler(self, event) -> None:
        self._handle(event)

    def _handle(self, event) -> bool:
        """True when `event` was the hotkey (and the callback has run)."""
        hotkey = self._hotkey
        if hotkey is None:
            return False
        try:
            if event.isARepeat():
                # Holding the key down must not toggle mute at the key-repeat
                # rate; only the initial press counts.
                return False
            if not hotkey.matches(event.keyCode(), event.modifierFlags()):
                return False
        except Exception:
            traceback.print_exc()
            return False
        self.triggers += 1
        try:
            self._on_trigger()
        except Exception:
            # An exception thrown out of an NSEvent handler unwinds into ObjC.
            traceback.print_exc()
        return True
