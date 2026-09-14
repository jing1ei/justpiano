"""Application paths and persisted user settings."""

from __future__ import annotations

import json
import math
import os
import threading
from typing import Callable, NamedTuple, Optional

from . import tone
from .reverb import PRESETS as REVERB_PRESETS

APP_NAME = "Just Piano"

_HOME = os.path.expanduser("~")

# JUSTPIANO_HOME relocates every file the app owns. The test suites set it so
# they can never clobber a real installation's settings or recordings.
_BASE = os.environ.get("JUSTPIANO_HOME")
if _BASE:
    SUPPORT_DIR = os.path.join(_BASE, "support")
    RECORDINGS_DIR = os.path.join(_BASE, "recordings")
else:
    SUPPORT_DIR = os.path.join(_HOME, "Library", "Application Support", APP_NAME)
    RECORDINGS_DIR = os.path.join(_HOME, "Music", APP_NAME)

CACHE_DIR = os.path.join(SUPPORT_DIR, "cache")
SETTINGS_PATH = os.path.join(SUPPORT_DIR, "settings.json")

DEFAULTS = {
    "volume": 0.75,
    "reverb": "room",           # off | room | hall
    "velocity_curve": "normal",  # soft | normal | hard
    # How far one strike differs from the next: 0 = every strike identical (what
    # this was before the setting existed), 1 = a real piano's spread, 2 = an
    # instrument overdue a tuning. No menu item; hand-edit it.
    "strike_variation": 1.0,
    # Sympathetic resonance: how loud the undamped strings answer under the
    # damper pedal. 0 = off (silent, and free), 1 = as measured on a grand.
    "resonance": 1.0,
    # How loud the recorded action is -- the damper landing when a key comes up,
    # and the pedal mechanism. 0 = silent, 1 = where a close pair of microphones
    # over the strings put it, which is much louder than it is worth playing
    # under. The Sound menu offers Off/Subtle/Natural/Prominent.
    "key_noise": 0.25,
    "voicing": tone.DEFAULT_VOICING,   # any key of tone.VOICINGS
    "blocksize": 256,            # 128 = lowest latency, 512 = safest
    "midi_port": None,           # remembered MIDI input port name
    "output_device": None,       # remembered audio output device name
    "login_item": False,
    # Global mute hotkey, "ctrl+alt+cmd+m" style (see hotkey.parse); None = off.
    # Mute itself is deliberately *not* persisted: an app that starts up silent
    # with no window to explain why is indistinguishable from a broken one.
    "mute_hotkey": "ctrl+alt+cmd+m",
}

#: Loudest gain the volume menu offers (its top step is 125%). A hand-edited
#: 9.5 is not a preference, it is a blown eardrum.
MAX_VOLUME = 1.25

#: Blocksize bounds, in frames. The menu offers 128/256/512; a power user may
#: reasonably want 64 or 1024, but 0 and -1 only stop the audio thread dead.
MIN_BLOCKSIZE = 32
MAX_BLOCKSIZE = 4096

#: Touch curves, mirroring `synth.VELOCITY_CURVES`. Spelled out rather than
#: imported because synth -> samplebank -> config would be a cycle.
VELOCITY_CURVES = ("soft", "normal", "hard")


def valid_voicing(value) -> str:
    """Coerce a stored voicing id to one the tone model still offers.

    A hand-edited settings.json -- or one left behind by a build that shipped a
    voicing this one has dropped or renamed -- must not wedge the app on a
    voicing whose parameters are gone: the menu would show no checkmark at all,
    and `SampleBank` would fall back to the default's samples while the UI kept
    claiming the missing voicing.
    """
    return value if value in tone.VOICINGS else tone.DEFAULT_VOICING


def _is_number(value) -> bool:
    """A JSON number, and not the `True` that would sneak through `isinstance`."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _name_or_none(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() != "")


class _Rule(NamedTuple):
    """One key's contract: what it accepts, and what to call that in an error."""

    ok: Callable[[object], bool]
    wants: str


#: Every persisted key, validated on the way in *and* on every assignment.
#: This is a menu-bar-only app: there is no window and no console, so a value a
#: reader will choke on has to be turned into the default plus a line in
#: `Settings.error` here, or the user has a launcher that does nothing at all
#: and no way to find out why.
_RULES: dict[str, _Rule] = {
    "volume": _Rule(lambda v: _is_number(v) and 0.0 <= v <= MAX_VOLUME,
                    f"a gain between 0 and {MAX_VOLUME}"),
    "reverb": _Rule(lambda v: v in REVERB_PRESETS,
                    "one of " + "/".join(sorted(REVERB_PRESETS))),
    "velocity_curve": _Rule(lambda v: v in VELOCITY_CURVES,
                            "one of " + "/".join(VELOCITY_CURVES)),
    "strike_variation": _Rule(lambda v: _is_number(v) and 0.0 <= v <= 2.0,
                              "a depth between 0 and 2"),
    "resonance": _Rule(lambda v: _is_number(v) and 0.0 <= v <= 2.0,
                       "a depth between 0 and 2"),
    "key_noise": _Rule(lambda v: _is_number(v) and 0.0 <= v <= 1.0,
                       "a level between 0 and 1"),
    "voicing": _Rule(lambda v: v == valid_voicing(v),
                     "a voicing this build ships"),
    "blocksize": _Rule(lambda v: (isinstance(v, int) and not isinstance(v, bool)
                                  and MIN_BLOCKSIZE <= v <= MAX_BLOCKSIZE),
                       f"a whole number of frames between {MIN_BLOCKSIZE} "
                       f"and {MAX_BLOCKSIZE}"),
    "midi_port": _Rule(_name_or_none, "a MIDI port name, or null for automatic"),
    "output_device": _Rule(_name_or_none,
                           "an output device name, or null for the system default"),
    "login_item": _Rule(lambda v: isinstance(v, bool), "true or false"),
    "mute_hotkey": _Rule(_name_or_none,
                         'a shortcut like "ctrl+alt+cmd+m", or null for none'),
}


def validate(key: str, value):
    """Return `(value, problem)`: the value to store, and what was wrong with it.

    Unknown keys pass through untouched -- `Settings` only ever stores keys that
    are in `DEFAULTS` -- and a value that fails its rule is replaced by the
    default, never dropped, so every reader still finds something usable.
    """
    rule = _RULES.get(key)
    if rule is None or rule.ok(value):
        return value, None
    default = DEFAULTS.get(key)
    return default, (f"{key}={value!r} is not {rule.wants}; "
                     f"using {default!r} instead")


def ensure_dirs() -> None:
    for path in (SUPPORT_DIR, CACHE_DIR, RECORDINGS_DIR):
        os.makedirs(path, exist_ok=True)


#: "settings.json was never read", as distinct from a file that holds `null`.
_UNREAD = object()


class Settings:
    def __init__(self) -> None:
        # Re-entrant: `__setitem__` holds it across the `save()` that persists
        # the mutation, and `save()` takes it too.
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        # Last thing worth telling the user about: a save() that failed, a
        # settings file that could not be read, or a stored value that had to be
        # replaced by its default (mirrors bank.error / engine.error). None while
        # everything is well.
        self.error: Optional[str] = None
        try:
            # Guarded like the one in save(): an unwritable support directory (a
            # stale file where the folder should be, a full disk) must not raise
            # out of the constructor, because at that point there is no menu bar
            # item, no alert and no console to carry the reason.
            ensure_dirs()
        except Exception as exc:
            self._note(f"{SUPPORT_DIR} is not usable: {exc}")

        stored = _UNREAD
        try:
            with open(SETTINGS_PATH) as fh:
                stored = json.load(fh)
        except FileNotFoundError:
            stored = {}          # first launch: defaults are not a fault
        except Exception as exc:
            self._note(f"{os.path.basename(SETTINGS_PATH)} could not be read "
                       f"({exc}); starting from the defaults")
            self._preserve_unreadable()
        if isinstance(stored, dict):
            mine = {k: v for k, v in stored.items() if k in DEFAULTS}
            if stored and not mine:
                # Valid JSON, but not our schema: some other program's file, or
                # one from a build with different keys. Silently defaulting made
                # the next menu click overwrite it.
                self._note(f"{os.path.basename(SETTINGS_PATH)} holds no Just Piano "
                           "settings; starting from the defaults")
                self._preserve_unreadable()
            self._data.update(mine)
        elif stored is not _UNREAD:
            # A list, a string, or a bare `null` -- readable JSON that is not an
            # object. `null` is why this is a sentinel and not just `is None`.
            self._note(f"{os.path.basename(SETTINGS_PATH)} holds "
                       f"{type(stored).__name__}, not an object; starting from "
                       "the defaults")
            self._preserve_unreadable()
        # Validated on the way in, not on the way out: every reader (tray menu,
        # SampleBank, AudioEngine, the cache file name) has to agree on one
        # value, and none of them is in a position to report a bad one.
        for key, value in list(self._data.items()):
            self._data[key], problem = validate(key, value)
            self._note(problem)

    def _note(self, problem: Optional[str]) -> None:
        """Add one line to `error`, keeping any earlier ones."""
        if not problem:
            return
        self.error = problem if not self.error else f"{self.error}; {problem}"

    def _preserve_unreadable(self) -> None:
        """Keep a copy of a settings file we could not use.

        The first `__setitem__` replaces settings.json wholesale, so without
        this a typo in a hand-edited file destroys every other choice in it --
        and the copy is what the user needs to fix the typo.
        """
        try:
            with open(SETTINGS_PATH, "rb") as fh:
                blob = fh.read()
            backup = SETTINGS_PATH + ".bak"
            with open(backup, "wb") as fh:
                fh.write(blob)
            self._note(f"a copy of it is kept as {os.path.basename(backup)}")
        except Exception:
            # Best effort: the unreadable file is a diagnostic, not the app.
            pass

    def __getitem__(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        # The mutation and the write that persists it stay one critical section:
        # json.dump must never iterate _data while another writer is adding a
        # key. `_lock` is re-entrant, so save() can take it too.
        with self._lock:
            value, problem = validate(key, value)
            self._data[key] = value
            self.save()
            # After save(), which clears `error` when the write goes through: a
            # value that had to be replaced is still worth saying out loud.
            self._note(problem)

    def save(self) -> None:
        tmp = f"{SETTINGS_PATH}.{os.getpid()}.tmp"
        try:
            with self._lock:
                ensure_dirs()
                # One tmp name per process: a single shared "settings.json.tmp"
                # meant two running copies of the app (a second launch, a second
                # bundle) replaced each other's half-written file -- 14% of
                # reads came back as unparsable JSON and one save() in five
                # failed ENOENT because its tmp had been renamed away.
                with open(tmp, "w") as fh:
                    json.dump(self._data, fh, indent=2)
                    # ...and the rename only promises to publish what reached
                    # the disk: without this a crash or power cut leaves an
                    # empty settings.json behind the directory entry.
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, SETTINGS_PATH)
                self.error = None
        except Exception as exc:
            # Never raise from a menu callback, but keep the reason so the UI
            # can tell the user their choices are not being remembered.
            self.error = str(exc)
            try:
                os.remove(tmp)
            except OSError:
                pass
