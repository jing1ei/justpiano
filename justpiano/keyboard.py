"""
Geometry and state of an 88-key piano keyboard - pure Python, no AppKit.

Everything the on-screen keyboard knows lives here so that it can be tested
headless: where every key is, which key a point lands on, how hard that point
means, which keys are down, and what has to be painted. `keyboardview` owns the
pixels and nothing else.

**The geometry.** MIDI 21 (A0) to 108 (C8): 52 white keys tiling the full width
edge to edge, 36 black keys sitting on top of them. Black keys are *not* centred
on the boundary between their two white neighbours - a real piano is built so
that the visible tops of the white keys inside each group (C-D-E, F-G-A-B) come
out the same width, and that is what fixes the offsets:

    a group of 3 whites and 2 blacks:  3*top + 2*b = 3*w  ->  top = w - 2b/3
    a group of 4 whites and 3 blacks:  4*top + 3*b = 4*w  ->  top = w - 3b/4

Writing each black key's left edge as "so many black-key widths left of the
white/white boundary it straddles" then gives `BLACK_SHIFT` below: C# and D#
lean 1/6 of a black width outwards, F# and A# lean 1/4 outwards, G# alone is
centred - the classic piano offsets, and the reason the C-D-E tops are wider
than the F-G-A-B ones.

**Coordinates.** x grows to the right from the left edge of A0; y grows from the
far end of the keys (y = 0, where the black keys are) towards the player
(y = height, the front edge). That is a *flipped* view in AppKit terms, which is
what `keyboardview.KeyboardView` declares itself to be, so the two never have to
translate between conventions. Clicking low on a key (large y) is loud; the top
of a key is the quietest place on it.
"""

from __future__ import annotations

import threading
from typing import Callable, Mapping, NamedTuple, Optional

from .tone import NOTE_MAX, NOTE_MIN   # one definition of "88 keys, A0..C8"

NOTE_COUNT = NOTE_MAX - NOTE_MIN + 1        # 88
WHITE_KEY_COUNT = 52
BLACK_KEY_COUNT = 36
MIDDLE_C = 60

#: Pitch classes (semitones above C) that are black keys.
BLACK_PITCH_CLASSES = frozenset((1, 3, 6, 8, 10))

#: A real grand: 13.7 mm of black key on a 23.5 mm white key, and the sharps
#: run about 62 % of the way down the naturals.
BLACK_WIDTH_RATIO = 13.7 / 23.5
BLACK_LENGTH_RATIO = 0.62

#: How far left of the white/white boundary each black key's *left* edge sits,
#: in black-key widths (see the module docstring for the derivation). 1/2 means
#: "centred on the boundary".
BLACK_SHIFT = {1: 2 / 3, 3: 1 / 3, 6: 3 / 4, 8: 1 / 2, 10: 1 / 4}

#: Default panel proportions: 52 * 17 pt is a hair under 900 pt wide, which
#: fits the built-in display of every Mac that can run the app.
WHITE_WIDTH = 17.0
KEY_LENGTH = 104.0

#: Velocity range a mouse click can produce, top of the key to the front edge.
MOUSE_VELOCITY_MIN = 40
MOUSE_VELOCITY_MAX = 127

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Colours (r, g, b, 0..1) the view paints with. Kept here so that "a lit key
# looks different" is a headless assertion rather than a screenshot.
WHITE_FILL = (0.97, 0.97, 0.95)
WHITE_EDGE = (0.42, 0.42, 0.45)
BLACK_FILL = (0.11, 0.11, 0.13)
BLACK_EDGE = (0.03, 0.03, 0.04)
WHITE_LIT = (0.36, 0.68, 1.00)
BLACK_LIT = (0.16, 0.46, 0.90)
LABEL_COLOR = (0.35, 0.35, 0.38)
MIDDLE_C_LABEL_COLOR = (0.85, 0.33, 0.20)
BACKGROUND = (0.16, 0.16, 0.18)


def is_black(note: int) -> bool:
    """True for a sharp/flat key."""
    return int(note) % 12 in BLACK_PITCH_CLASSES


def note_name(note: int) -> str:
    """Scientific pitch name: 21 -> "A0", 60 -> "C4", 61 -> "C#4"."""
    note = int(note)
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def _build_white_index() -> dict[int, int]:
    index, table = 0, {}
    for note in range(NOTE_MIN, NOTE_MAX + 1):
        if not is_black(note):
            table[note] = index
            index += 1
    return table


_WHITE_INDEX = _build_white_index()
#: The 52 white notes, in order, so a white index maps straight back to a note.
WHITE_NOTES = tuple(sorted(_WHITE_INDEX, key=_WHITE_INDEX.get))
BLACK_NOTES = tuple(n for n in range(NOTE_MIN, NOTE_MAX + 1) if is_black(n))
#: Every C in range, for the octave labels.
LABEL_NOTES = tuple(n for n in WHITE_NOTES if n % 12 == 0)


def white_index(note: int) -> int:
    """Position of a white key among the 52, counting A0 as 0."""
    try:
        return _WHITE_INDEX[int(note)]
    except KeyError:
        raise ValueError(f"{note} is not a white key of an 88-key piano") from None


class KeyRect(NamedTuple):
    """One key's rectangle, in keyboard coordinates."""

    note: int
    x: float
    y: float
    width: float
    height: float
    black: bool

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """The front edge of the key - the end nearest the player."""
        return self.y + self.height

    @property
    def center_x(self) -> float:
        return self.x + self.width * 0.5

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom


class DrawOp(NamedTuple):
    """One paint instruction: a key rectangle, or a label centred on one."""

    kind: str                                   # "key" | "label"
    note: int
    x: float
    y: float
    width: float
    height: float
    fill: tuple[float, float, float]
    stroke: Optional[tuple[float, float, float]] = None
    text: str = ""


def _blend(base, lit, amount: float) -> tuple[float, float, float]:
    amount = max(0.0, min(1.0, amount))
    return tuple(b + (l - b) * amount for b, l in zip(base, lit))  # type: ignore[return-value]


def lit_color(note: int, velocity: int) -> tuple[float, float, float]:
    """Colour of a sounding key: harder playing shows up brighter."""
    black = is_black(note)
    amount = 0.55 + 0.45 * max(0, min(127, int(velocity))) / 127.0
    return _blend(BLACK_FILL if black else WHITE_FILL,
                  BLACK_LIT if black else WHITE_LIT, amount)


class Keyboard:
    """The 88 key rectangles for a given panel size."""

    def __init__(self, width: float = WHITE_WIDTH * WHITE_KEY_COUNT,
                 height: float = KEY_LENGTH) -> None:
        if not (width > 0 and height > 0):
            raise ValueError(f"keyboard must have a positive size, got {width}x{height}")
        self.width = float(width)
        self.height = float(height)
        self.white_width = self.width / WHITE_KEY_COUNT
        self.black_width = self.white_width * BLACK_WIDTH_RATIO
        self.black_height = self.height * BLACK_LENGTH_RATIO

        rects: dict[int, KeyRect] = {}
        for note in range(NOTE_MIN, NOTE_MAX + 1):
            pitch_class = note % 12
            if pitch_class in BLACK_PITCH_CLASSES:
                # The boundary this sharp straddles is the right edge of the
                # white key a semitone below it (C for C#, F for F# ...).
                boundary = (white_index(note - 1) + 1) * self.white_width
                left = boundary - BLACK_SHIFT[pitch_class] * self.black_width
                rects[note] = KeyRect(note, left, 0.0, self.black_width,
                                      self.black_height, True)
            else:
                rects[note] = KeyRect(note, white_index(note) * self.white_width,
                                      0.0, self.white_width, self.height, False)

        self._rects = rects
        self.keys = tuple(rects[n] for n in range(NOTE_MIN, NOTE_MAX + 1))
        self.white_keys = tuple(rects[n] for n in WHITE_NOTES)
        self.black_keys = tuple(rects[n] for n in BLACK_NOTES)
        #: Whites first: the blacks are painted over them, as they overlap.
        self.draw_order = self.white_keys + self.black_keys

    # ------------------------------------------------------------- geometry
    def rect(self, note: int) -> KeyRect:
        try:
            return self._rects[int(note)]
        except KeyError:
            raise ValueError(f"note {note} is outside an 88-key piano "
                             f"({NOTE_MIN}-{NOTE_MAX})") from None

    def note_at(self, x: float, y: float) -> Optional[int]:
        """The note under a point, or None when the point misses the keyboard.

        Black keys win wherever they overlap a white one - they are on top, and
        that is precisely where a naive left-to-right scan plays the wrong note.
        """
        if not (0.0 <= x < self.width and 0.0 <= y < self.height):
            return None
        if y < self.black_height:
            for key in self.black_keys:
                if x < key.x:
                    break            # ordered by x: no later black can match
                if x < key.right:
                    return key.note
        index = min(int(x // self.white_width), WHITE_KEY_COUNT - 1)
        return WHITE_NOTES[index]

    def velocity_at(self, note: int, y: float) -> int:
        """Velocity for a click at height `y` on `note`: the front edge of the
        key is the loudest place on it, the far end the quietest."""
        key = self.rect(note)
        fraction = (y - key.y) / key.height
        fraction = max(0.0, min(1.0, fraction))
        return int(round(MOUSE_VELOCITY_MIN
                         + (MOUSE_VELOCITY_MAX - MOUSE_VELOCITY_MIN) * fraction))

    def hit(self, x: float, y: float) -> Optional[tuple[int, int]]:
        """(note, velocity) for a click, or None if it missed every key."""
        note = self.note_at(x, y)
        if note is None:
            return None
        return note, self.velocity_at(note, y)


def draw_plan(keyboard: Keyboard,
              lit: Optional[Mapping[int, int]] = None) -> list[DrawOp]:
    """What to paint, in order: white keys, black keys on top, octave labels.

    `lit` maps a sounding note to the velocity it was played with, which is what
    makes a hard chord glow brighter than a brushed one.
    """
    lit = lit or {}
    ops: list[DrawOp] = []
    for key in keyboard.draw_order:
        velocity = lit.get(key.note)
        if velocity is None:
            fill = BLACK_FILL if key.black else WHITE_FILL
        else:
            fill = lit_color(key.note, velocity)
        ops.append(DrawOp("key", key.note, key.x, key.y, key.width, key.height,
                          fill, BLACK_EDGE if key.black else WHITE_EDGE))
    for note in LABEL_NOTES:
        key = keyboard.rect(note)
        ops.append(DrawOp("label", note, key.x, key.y, key.width, key.height,
                          MIDDLE_C_LABEL_COLOR if note == MIDDLE_C else LABEL_COLOR,
                          None, note_name(note)))
    return ops


class NoteLights:
    """Which keys are down and how hard, written from the MIDI callback thread.

    The rtmidi callback (and the audio thread's owner, the main thread) must
    never touch AppKit, so this is the handover point: the callback does one
    O(1) dict write under a lock it holds for a few instructions, and the main
    thread's redraw timer takes a snapshot. `version` lets that timer notice
    "nothing moved" without copying anything at all.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._down: dict[int, int] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """Bumped on every actual change, never on a no-op."""
        return self._version

    def press(self, note: int, velocity: int = 64) -> None:
        with self._lock:
            self._down[int(note)] = int(velocity)
            self._version += 1

    def release(self, note: int) -> None:
        with self._lock:
            if self._down.pop(int(note), None) is not None:
                self._version += 1

    def release_all(self) -> None:
        """Panic / All Notes Off: every key comes back up."""
        with self._lock:
            if self._down:
                self._down.clear()
                self._version += 1

    def is_down(self, note: int) -> bool:
        return int(note) in self._down

    def snapshot(self) -> dict[int, int]:
        """A private copy for the main thread to draw from."""
        with self._lock:
            return dict(self._down)

    def __len__(self) -> int:
        return len(self._down)


class KeyboardController:
    """Everything the on-screen keyboard does apart from drawing.

    Holds the geometry, the lit keys and the mouse gesture, and reports notes
    through plain callbacks so the tray can feed them into the same path a MIDI
    keyboard uses. The AppKit view forwards its three mouse events here; the
    tests drive exactly the same three methods.
    """

    def __init__(self, keyboard: Optional[Keyboard] = None, *,
                 on_note_on: Optional[Callable[[int, int], None]] = None,
                 on_note_off: Optional[Callable[[int], None]] = None,
                 on_settings: Optional[Callable[[], None]] = None,
                 on_mute: Optional[Callable[[], None]] = None) -> None:
        self.keyboard = keyboard if keyboard is not None else Keyboard()
        self.lights = NoteLights()
        self._on_note_on = on_note_on
        self._on_note_off = on_note_off
        self._on_settings = on_settings
        self._on_mute = on_mute
        #: The keys the view is currently drawing as lit (a main-thread copy).
        self.lit: dict[int, int] = {}
        self._lit_version = -1
        #: The note the mouse is holding down, if any.
        self.mouse_note: Optional[int] = None
        #: What the panel's mute button has to show. A mirror of the engine's
        #: state, written by the tray on the main thread whichever of the three
        #: entry points asked for the change, so the button can never disagree
        #: with the menu checkmark or the menu bar icon.
        self.muted = False

    # --------------------------------------------------------------- lights
    def refresh(self) -> bool:
        """Take a new snapshot of the lit keys. True when the view must redraw."""
        version = self.lights.version
        if version == self._lit_version:
            return False
        self._lit_version = version
        self.lit = self.lights.snapshot()
        return True

    # ---------------------------------------------------------------- mouse
    def mouse_down(self, x: float, y: float) -> Optional[int]:
        hit = self.keyboard.hit(x, y)
        if hit is None:
            self.mouse_up()
            return None
        note, velocity = hit
        self._press(note, velocity)
        return note

    def mouse_dragged(self, x: float, y: float) -> Optional[int]:
        """Glissando: sliding onto another key releases the old one and plays
        the new one. Wandering off the keyboard holds the note that is down
        rather than re-triggering it on the way back."""
        if self.mouse_note is None:
            return None
        hit = self.keyboard.hit(x, y)
        if hit is None:
            return self.mouse_note
        note, velocity = hit
        if note == self.mouse_note:
            return note
        self._press(note, velocity)
        return note

    def mouse_up(self) -> None:
        """Let go of whatever the mouse was playing (idempotent)."""
        note, self.mouse_note = self.mouse_note, None
        if note is not None and self._on_note_off is not None:
            self._on_note_off(note)

    def _press(self, note: int, velocity: int) -> None:
        if self.mouse_note is not None and self.mouse_note != note:
            self.mouse_up()
        self.mouse_note = note
        if self._on_note_on is not None:
            self._on_note_on(note, velocity)

    # ------------------------------------------------------------- settings
    def settings_clicked(self) -> None:
        """The gear button was clicked: hand over to the tray's menu."""
        if self._on_settings is not None:
            self._on_settings()

    # ----------------------------------------------------------------- mute
    def mute_clicked(self) -> None:
        """The panel's mute button was clicked: hand over to the tray.

        The button reports the *click*, never a state of its own: the tray owns
        the mute state and calls `set_muted()` back once the engine has it, so a
        refused or coalesced toggle cannot leave the button lying.
        """
        if self._on_mute is not None:
            self._on_mute()

    def set_muted(self, muted: bool) -> bool:
        """Remember what the mute button must show. True when it changed."""
        muted = bool(muted)
        if muted == self.muted:
            return False
        self.muted = muted
        return True

    # --------------------------------------------------------------- paint
    def draw_plan(self) -> list[DrawOp]:
        return draw_plan(self.keyboard, self.lit)
