"""
Integration smoke test for the menu bar app.

macOS-only libraries (rumps/AppKit, PortAudio, CoreMIDI) are replaced with
stubs that mirror the real APIs, so the whole app -- menu construction, MIDI
dispatch, the audio callback, recording and both exporters -- can be exercised
on any machine.

The stubs are deliberately never *more* permissive than the real libraries: a
stub that cannot fail (or that accepts what rumps rejects) is how a real-Mac
crash slips through a green run.

    python3 -m tools.tray_smoke
"""

from __future__ import annotations

import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

# Keep the suite away from a real installation's settings/recordings, and force
# it: with `setdefault` a pre-set (or reused) JUSTPIANO_HOME would make the
# "settings persist to disk" check re-read the previous run's identical values,
# and Settings.save() swallows every error. Must stay above the first
# `justpiano` import -- `config` reads the variable at import time.
_HOME = _tempfile.mkdtemp(prefix="justpiano-home-")
_os.environ["JUSTPIANO_HOME"] = _HOME
_atexit.register(_shutil.rmtree, _HOME, ignore_errors=True)

import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import types
import wave
import weakref

import numpy as np

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))


def wait_until(predicate, timeout, interval=0.05):
    """Poll `predicate` until it is true or `timeout` seconds have passed.

    Everything in this suite that waits for another thread uses this instead of
    a fixed sleep: the audio stub renders in real time and drops blocks when it
    falls behind, so a sleep calibrated on an idle machine is a coin toss on a
    busy one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def click_if_enabled(item) -> bool:
    """Click a menu item unless it is greyed out.

    A disabled item raises from click(), which would abort the whole run: the
    checks around each call assert the effect anyway, so a regression that
    greys an item out should be one FAIL, not a lost suite.
    """
    if item.callback is None:
        return False
    item.click()
    return True


@contextlib.contextmanager
def quiet_stderr():
    """Capture stderr, so a deliberately injected AppKit failure does not print a
    traceback in the middle of the results.

    `keyboardview` reports what it swallows with `traceback.print_exc()`, and the
    text it printed is itself worth asserting: a failure that is silently
    swallowed is the one shape of "degrade, never raise" that is indistinguishable
    from a no-op.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


# ================================================ fake Objective-C runtime ==
# Everything below stands in for AppKit/Foundation, and is what lets the *real*
# `justpiano.keyboardview` -- all 730 lines of it, popover, views, buttons,
# delegate and status-item plumbing -- run on a machine without pyobjc.
#
# The precedent is `_NSEventStub` further down: fake the objects, keep the
# contract. The contract that matters here is Objective-C *message dispatch*,
# because that is the part of this app that Python cannot check for you: a
# target/action pair is a selector *name*, so a renamed method, a mistyped
# selector or the wrong number of colons is not an ImportError or a failing
# unit test - it is `doesNotRecognizeSelector:` the first time a user clicks the
# menu bar icon on a real Mac. So `_objc_send` resolves selectors the way the
# runtime does, and every target/action here goes through it.
class _ObjCError(Exception):
    """An Objective-C exception as pyobjc re-raises it (objc.error/NSException).

    Used for what ObjC signals at runtime rather than what Python's own type
    system would catch: an unrecognised selector, a popover anchored to a view
    with no window, a duplicate class name.
    """


#: Every ObjC class defined in this process. `objc_allocateClassPair` fails for
#: a name that is already registered, which is exactly why
#: `keyboardview._build_classes()` caches its result (including its failure) and
#: can only be run once - so the fake runtime enforces it too.
_OBJC_CLASSES: dict = {}


def _selector_arity(selector) -> int:
    """How many arguments a selector takes, i.e. how many colons it has."""
    if not isinstance(selector, str):
        raise TypeError(f"a selector name must be a string, not "
                        f"{type(selector).__name__}")
    return selector.count(":")


def _objc_send(receiver, selector, *args):
    """Send `selector` to `receiver`, the way the ObjC runtime would.

    * a message to nil is a no-op that answers nil (AppKit holds targets and
      delegates *weakly*, so this is a state the app really reaches);
    * the number of arguments has to match the number of colons;
    * a receiver that does not implement the selector raises, rather than the
      Python-level AttributeError a `getattr(obj, name, None)` would swallow.

    That last point is the whole reason this exists: `performClick_` used to be
    a counter on a stub, so the selector string it was supposed to send was
    never resolved against anything at all.
    """
    if receiver is None:
        return None
    arity = _selector_arity(selector)
    if arity != len(args):
        raise _ObjCError(f"selector {selector!r} takes {arity} argument(s), "
                         f"{len(args)} given")
    method = getattr(receiver, selector.replace(":", "_"), None)
    if not callable(method):
        raise _ObjCError(f"-[{type(receiver).__name__} {selector}]: unrecognized "
                         f"selector sent to instance")
    return method(*args)


class _ObjCObject:
    """Foundation's NSObject, in the shape pyobjc presents it.

    `alloc()` hands back an uninitialised instance and `init...` returns self,
    so the two-step construction the real code uses is the one under test.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        name = cls.__name__
        if name in _OBJC_CLASSES:
            raise _ObjCError(f"Objective-C class {name} is already registered")
        _OBJC_CLASSES[name] = cls

    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def init(self):
        return self

    def respondsToSelector_(self, selector):
        _selector_arity(selector)       # a selector is a string, colons and all
        return callable(getattr(self, selector.replace(":", "_"), None))

    def performSelector_withObject_afterDelay_(self, selector, obj, delay):
        """Queue a message for a later pass of the *default* run loop mode.

        Faithful in the two ways `keyboardview.pop_up_menu` depends on: the
        message does not run here (that is the entire point - it must not happen
        inside the caller's event handling), and the receiver is retained until
        it has been performed, so the caller may drop it on the floor.
        """
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            raise TypeError("afterDelay: takes a number of seconds")
        if not self.respondsToSelector_(selector):
            raise _ObjCError(f"-[{type(self).__name__} {selector}]: unrecognized "
                             f"selector sent to instance")
        _RUNLOOP.append((time.monotonic() + float(delay), self, selector, obj))


#: What `performSelector:withObject:afterDelay:` has queued and the run loop has
#: not come back for yet: (due, receiver, selector, argument).
_RUNLOOP: list = []


def _pump_runloop(passes: int = 1) -> int:
    """Run one pass of the fake run loop. Returns how many selectors fired.

    Only what was queued *before* the pass started is performed, because that is
    how the real thing behaves: a selector scheduled from inside a performed
    selector waits for the next pass.
    """
    fired = 0
    for _ in range(passes):
        due, _RUNLOOP[:] = list(_RUNLOOP), []
        for _at, receiver, selector, obj in due:
            _objc_send(receiver, selector, obj)
            fired += 1
    return fired


# ------------------------------------------------------------------ geometry
class _NSPoint:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x, self.y = _require_number(x), _require_number(y)

    def __eq__(self, other):
        return (isinstance(other, _NSPoint) and self.x == other.x
                and self.y == other.y)

    def __repr__(self):
        return f"NSPoint({self.x:g}, {self.y:g})"


class _NSSize:
    __slots__ = ("width", "height")

    def __init__(self, width, height):
        self.width = _require_number(width)
        self.height = _require_number(height)

    def __eq__(self, other):
        return (isinstance(other, _NSSize) and self.width == other.width
                and self.height == other.height)

    def __repr__(self):
        return f"NSSize({self.width:g}, {self.height:g})"


class _NSRect:
    __slots__ = ("origin", "size")

    def __init__(self, x, y, width, height):
        self.origin = _NSPoint(x, y)
        self.size = _NSSize(width, height)

    def as_tuple(self):
        return (self.origin.x, self.origin.y, self.size.width, self.size.height)

    def __eq__(self, other):
        return isinstance(other, _NSRect) and self.as_tuple() == other.as_tuple()

    def __repr__(self):
        return "NSRect({:g}, {:g}, {:g}, {:g})".format(*self.as_tuple())


def _require_number(value):
    """NSMakeRect and friends take CGFloats: pyobjc raises for anything else,
    and a string sneaking into a frame is a crash only a Mac would show."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"a number is required but given {value!r}, "
                        f"a {type(value).__name__}")
    return float(value)


def _NSMakeRect(x, y, width, height):
    return _NSRect(x, y, width, height)


def _NSMakePoint(x, y):
    return _NSPoint(x, y)


def _NSMakeSize(width, height):
    return _NSSize(width, height)


class _NSWindow(_ObjCObject):
    """Only what `keyboardview._click_landed_on` asks of one: its frame on
    screen, in the coordinates `NSEvent.mouseLocation()` reports."""

    def __init__(self, frame):
        if not isinstance(frame, _NSRect):
            raise TypeError("a window frame is an NSRect")
        self._frame = frame

    def frame(self):
        return self._frame


# --------------------------------------------------------------- NSView & co
class _NSViewStub(_ObjCObject):
    """NSView: a frame, subviews, a window and a display flag."""

    def initWithFrame_(self, frame):
        if not isinstance(frame, _NSRect):
            raise TypeError("initWithFrame: requires an NSRect, not "
                            f"{type(frame).__name__}")
        self._frame = frame
        self._subviews: list = []
        self._superview = None
        self._window = None
        self.display_requests = 0
        self.draw_calls = 0
        return self

    # rumps' status item button is built directly rather than allocated, so the
    # frame has to be settable from __init__ too. Bound to NSView's own
    # implementation, never to a subclass override, or an NSButton (which
    # initialises its control half first) would recurse.
    def _init_view(self, frame):
        return _NSViewStub.initWithFrame_(self, frame)

    def frame(self):
        return self._frame

    def setFrame_(self, frame):
        if not isinstance(frame, _NSRect):
            raise TypeError("setFrame: requires an NSRect")
        self._frame = frame

    def bounds(self):
        return _NSRect(0.0, 0.0, self._frame.size.width, self._frame.size.height)

    def isFlipped(self):
        return False

    def addSubview_(self, view):
        if not isinstance(view, _NSViewStub):
            raise TypeError("addSubview: requires an NSView, not "
                            f"{type(view).__name__}")
        self._subviews.append(view)
        view._superview = self

    def subviews(self):
        return list(self._subviews)

    def superview(self):
        return self._superview

    def window(self):
        view = self
        while view is not None:
            if view._window is not None:
                return view._window
            view = view._superview
        return None

    def setNeedsDisplay_(self, flag):
        if not isinstance(flag, (bool, int)):
            raise TypeError("setNeedsDisplay: takes a BOOL")
        if flag:
            self.display_requests += 1

    def convertPoint_fromView_(self, point, view):
        """Window (or `view`) coordinates -> this view's own coordinates.

        Honours `isFlipped`, which is not decoration: `keyboard.py` measures y
        from the far end of the keys towards the player, so a view that stopped
        being flipped would turn every click into the wrong velocity (and, near
        the black keys, the wrong note).
        """
        if not isinstance(point, _NSPoint):
            raise TypeError("convertPoint:fromView: requires an NSPoint")
        if not (view is None or isinstance(view, _NSViewStub)):
            raise TypeError("convertPoint:fromView: takes a view or nil")
        x, y = point.x, point.y
        if view is not None:
            node = view
            while node is not None:                 # -> window coordinates
                x += node._frame.origin.x
                y += node._frame.origin.y
                node = node._superview
        node = self
        while node is not None:                     # window -> self
            x -= node._frame.origin.x
            y -= node._frame.origin.y
            node = node._superview
        if self.isFlipped():
            y = self._frame.size.height - y
        return _NSPoint(x, y)

    def acceptsFirstMouse_(self, event):
        return False

    def drawRect_(self, rect):
        pass


class _NSControlStub(_NSViewStub):
    """NSControl's target/action half.

    The target is held **weakly**, exactly as AppKit holds it - which is why
    `intercept_status_item` documents that the caller must keep the handler, and
    why that is now a thing this suite can check.
    """

    def _init_control(self, frame):
        self._init_view(frame)
        self._target_ref = None
        self.action = None
        self.action_mask = None
        self.clicks = 0
        return self

    @property
    def target(self):
        return None if self._target_ref is None else self._target_ref()

    def setTarget_(self, target):
        self._target_ref = None if target is None else weakref.ref(target)

    def setAction_(self, action):
        # pyobjc turns a string into a selector; anything else is a TypeError,
        # and a silently accepted non-string would only crash on a Mac.
        if not isinstance(action, str):
            raise TypeError("selector name must be a string, not "
                            f"{type(action).__name__}")
        self.action = action

    def sendActionOn_(self, mask):
        """NSControl.sendActionOn: - takes an NSEventMask, returns the old one."""
        if isinstance(mask, bool) or not isinstance(mask, int):
            raise TypeError("NSEventMask must be an integer")
        previous, self.action_mask = self.action_mask, int(mask)
        return previous or 0

    def performClick_(self, sender):
        """Send the action to the target, by selector name.

        A target that does not implement the action raises here, the way the
        runtime does - which is what makes a renamed handler a red check instead
        of a crash on somebody's Mac. An action of nil, or a target that has been
        collected, is a no-op.
        """
        self.clicks += 1
        if self.action is None:
            return None
        return _objc_send(self.target, self.action, sender)


class _NSButtonStub(_NSControlStub):
    """NSButton, as the panel's gear and mute buttons are configured."""

    def initWithFrame_(self, frame):
        self._init_control(frame)
        self.bordered = True
        self.tooltip = None
        self._image = None
        self._title = ""
        return self

    def setBordered_(self, flag):
        if not isinstance(flag, bool):
            raise TypeError("setBordered: takes a BOOL")
        self.bordered = flag

    def setToolTip_(self, text):
        _require_string_or_none(text)
        self.tooltip = text

    def setImage_(self, image):
        if not (image is None or isinstance(image, _NSImage)):
            raise TypeError("setImage: requires an NSImage or nil, not "
                            f"{type(image).__name__}")
        self._image = image

    def setTitle_(self, title):
        # -[NSButton setTitle:] with nil throws; the empty string is how you
        # clear it, which is what the panel does once it has an image.
        _require_string(title)
        self._title = title

    def image(self):
        return self._image

    def title(self):
        return self._title


class _NSTextFieldStub(_NSViewStub):
    """NSTextField, reduced to the label the panel puts in its header."""

    @classmethod
    def labelWithString_(cls, text):
        _require_string(text)
        label = cls.alloc().initWithFrame_(_NSRect(0.0, 0.0, 0.0, 0.0))
        label._string = text
        label.text_color = None
        return label

    def stringValue(self):
        return self._string

    def setTextColor_(self, color):
        if not isinstance(color, _NSColorStub):
            raise TypeError("setTextColor: requires an NSColor")
        self.text_color = color


class _NSViewControllerStub(_ObjCObject):
    def init(self):
        self._view = None
        return self

    def setView_(self, view):
        if not isinstance(view, _NSViewStub):
            raise TypeError("setView: requires an NSView")
        self._view = view

    def view(self):
        return self._view


class _NSNotification(_ObjCObject):
    def __init__(self, name, obj):
        self._name, self._object = name, obj

    def name(self):
        return self._name

    def object(self):
        return self._object


class _NSPopoverStub(_ObjCObject):
    """NSPopover, with the two behaviours the panel is built on.

    * `showRelativeToRect:ofView:preferredEdge:` needs a *window* behind the
      view it is anchored to, and throws without one (which is exactly the
      failure `KeyboardPanel.open()` returns False for);
    * closing a popover that is on screen notifies the delegate with
      `popoverDidClose:`, and closing one that is not notifies nobody. The
      transient dismissal AppKit does on its own goes through the same path -
      see `dismiss_transient()`.

    `delegate` is a **weak** property, like the real one: a panel that forgot to
    keep its delegate referenced would stop hearing about dismissals, and that
    is a bug this can see.
    """

    BEHAVIOR_APPLICATION_DEFINED = 0
    BEHAVIOR_TRANSIENT = 1
    BEHAVIOR_SEMITRANSIENT = 2

    def init(self):
        self._content_view_controller = None
        self._content_size = None
        self._behavior = self.BEHAVIOR_APPLICATION_DEFINED
        self._animates = True
        self._delegate_ref = None
        self._shown = False
        #: (rect, view, edge) of every show, in order.
        self.shows: list = []
        self.closes = 0
        #: Failure injection: an anchor view whose window has gone away.
        self.fail_show = False
        return self

    def setContentViewController_(self, controller):
        if not isinstance(controller, _NSViewControllerStub):
            raise TypeError("setContentViewController: requires an "
                            "NSViewController")
        self._content_view_controller = controller

    def contentViewController(self):
        return self._content_view_controller

    def setContentSize_(self, size):
        if not isinstance(size, _NSSize):
            raise TypeError("setContentSize: requires an NSSize")
        self._content_size = size

    def contentSize(self):
        return self._content_size

    def setBehavior_(self, behavior):
        if isinstance(behavior, bool) or not isinstance(behavior, int):
            raise TypeError("setBehavior: takes an NSPopoverBehavior")
        if behavior not in (self.BEHAVIOR_APPLICATION_DEFINED,
                            self.BEHAVIOR_TRANSIENT,
                            self.BEHAVIOR_SEMITRANSIENT):
            raise _ObjCError(f"unknown NSPopoverBehavior {behavior}")
        self._behavior = behavior

    def behavior(self):
        return self._behavior

    def setAnimates_(self, flag):
        if not isinstance(flag, bool):
            raise TypeError("setAnimates: takes a BOOL")
        self._animates = flag

    def animates(self):
        return self._animates

    def setDelegate_(self, delegate):
        self._delegate_ref = None if delegate is None else weakref.ref(delegate)

    def delegate(self):
        return None if self._delegate_ref is None else self._delegate_ref()

    def isShown(self):
        return self._shown

    def showRelativeToRect_ofView_preferredEdge_(self, rect, view, edge):
        if not isinstance(rect, _NSRect):
            raise TypeError("showRelativeToRect: requires an NSRect, not "
                            f"{type(rect).__name__}")
        if not isinstance(view, _NSViewStub):
            raise TypeError("ofView: requires an NSView, not "
                            f"{type(view).__name__}")
        if isinstance(edge, bool) or not isinstance(edge, int):
            raise TypeError("preferredEdge: takes an NSRectEdge")
        if edge not in (0, 1, 2, 3):
            raise _ObjCError(f"unknown NSRectEdge {edge}")
        if self.fail_show or view.window() is None:
            raise _ObjCError("Attempt to show a popover relative to a view "
                             "with no window")
        self.shows.append((rect, view, edge))
        self._shown = True

    def close(self):
        self.closes += 1
        self._notify_close()

    def performClose_(self, _sender):
        self.close()

    # ------------------------------------------------------------ driving it
    def dismiss_transient(self):
        """What NSPopoverBehaviorTransient does by itself on a mouse-down
        outside the popover - the event the status item has not reported yet."""
        if self._behavior != self.BEHAVIOR_TRANSIENT:
            raise AssertionError("only a transient popover dismisses itself")
        self._notify_close()

    def _notify_close(self):
        if not self._shown:
            return              # AppKit sends nothing for a popover that is down
        self._shown = False
        _objc_send(self.delegate(), "popoverDidClose:",
                   _NSNotification("NSPopoverDidCloseNotification", self))


# --------------------------------------------------------- drawing & images
class _NSImage:
    """NSImage: either loaded from a file (rumps' status item icon) or made from
    an SF Symbol name (the panel's gear and mute buttons)."""

    def __init__(self, path=None, size=(20, 20), template=None, *,
                 symbol=None, description=None):
        self.path = path
        self.size = tuple(size)
        self.template = template
        self.symbol = symbol
        self.description = description

    @classmethod
    def imageWithSystemSymbolName_accessibilityDescription_(cls, name, description):
        """macOS 11+. On anything older the selector does not exist at all, and
        pyobjc raises AttributeError - which is the fallback path the panel's
        text glyphs exist for, so it is switchable here (`_AK.sf_symbols`)."""
        if not _AK.sf_symbols:
            raise AttributeError("NSImage has no attribute "
                                 "'imageWithSystemSymbolName_accessibilityDescription_'")
        _require_string(name)
        _require_string_or_none(description)
        if name not in _AK.known_symbols:
            # An unknown symbol name answers nil rather than throwing: the same
            # "no image" the caller has to cope with on macOS 10.
            return None
        return cls(symbol=name, description=description)

    def __repr__(self):
        what = self.symbol or self.path
        return f"<NSImage {what!r} template={self.template}>"


class _NSColorStub(_ObjCObject):
    def __init__(self, rgba, name=None):
        self.rgba = rgba
        self.name = name

    @classmethod
    def colorWithCalibratedRed_green_blue_alpha_(cls, red, green, blue, alpha):
        return cls(tuple(_require_number(v) for v in (red, green, blue, alpha)))

    @classmethod
    def secondaryLabelColor(cls):
        return cls(None, name="secondaryLabelColor")

    def setFill(self):
        _AK.fill = self

    def setStroke(self):
        _AK.stroke = self

    def __repr__(self):
        return f"<NSColor {self.name or self.rgba}>"


class _NSBezierPathStub(_ObjCObject):
    """NSBezierPath, recording what was painted with which colour.

    The fill and stroke colours come from the *graphics state*, not from the
    path, so a `setFill()` that went missing would paint the previous colour -
    which is why `_AK.fill` is read at fill() time rather than at set time.
    """

    def __init__(self, rect):
        self.rect = rect
        self.line_width = 1.0

    @classmethod
    def bezierPathWithRoundedRect_xRadius_yRadius_(cls, rect, x_radius, y_radius):
        if not isinstance(rect, _NSRect):
            raise TypeError("bezierPathWithRoundedRect: requires an NSRect")
        _require_number(x_radius), _require_number(y_radius)
        return cls(rect)

    @classmethod
    def fillRect_(cls, rect):
        if not isinstance(rect, _NSRect):
            raise TypeError("fillRect: requires an NSRect")
        _AK.painted.append(("fillRect", rect, _AK.fill))

    def setLineWidth_(self, width):
        self.line_width = _require_number(width)

    def fill(self):
        _AK.painted.append(("fill", self.rect, _AK.fill))

    def stroke(self):
        _AK.painted.append(("stroke", self.rect, _AK.stroke))


class _NSFontStub(_ObjCObject):
    def __init__(self, size):
        self.size = size

    @classmethod
    def systemFontOfSize_(cls, size):
        return cls(_require_number(size))


class _NSAttributedStringStub(_ObjCObject):
    """NSAttributedString: enough of one to lay out and draw the octave labels."""

    def initWithString_attributes_(self, string, attributes):
        _require_string(string)
        if not isinstance(attributes, dict):
            raise TypeError("initWithString:attributes: takes a dictionary")
        for key in attributes:
            if not isinstance(key, str):
                raise TypeError("attribute keys are NSStrings")
        font = attributes.get("NSFont")
        if font is not None and not isinstance(font, _NSFontStub):
            raise TypeError("NSFontAttributeName must be an NSFont")
        color = attributes.get("NSColor")
        if color is not None and not isinstance(color, _NSColorStub):
            raise TypeError("NSForegroundColorAttributeName must be an NSColor")
        self._string = string
        self._attributes = attributes
        return self

    def string(self):
        return self._string

    def size(self):
        font_size = getattr(self._attributes.get("NSFont"), "size", 12.0)
        return _NSSize(0.62 * font_size * len(self._string), font_size * 1.25)

    def drawAtPoint_(self, point):
        if not isinstance(point, _NSPoint):
            raise TypeError("drawAtPoint: requires an NSPoint")
        _AK.painted.append(("text", self._string, point))


# ----------------------------------------------------- NSApplication, NSEvent
class _ObjCEvent(_ObjCObject):
    """NSEvent, as `_is_secondary_click` and `_click_landed_on` read one."""

    def __init__(self, kind, location=None, flags=0):
        self._kind = int(kind)
        self._location = location or _NSPoint(0.0, 0.0)
        self._flags = int(flags)

    def type(self):
        return self._kind

    def modifierFlags(self):
        return self._flags

    def locationInWindow(self):
        return self._location


class _NSApplicationStub(_ObjCObject):
    """NSApplication: the current event, and being brought forward.

    macOS 14 added `activate`; `keyboardview._activate` prefers it and falls
    back to `activateIgnoringOtherApps:`, so both shapes exist here (see
    `_ak_legacy_activation`).
    """

    _shared = None

    def __init__(self):
        self.current_event = None
        self.activations: list = []

    @classmethod
    def sharedApplication(cls):
        if _NSApplicationStub._shared is None:
            _NSApplicationStub._shared = cls()
        return _NSApplicationStub._shared

    def currentEvent(self):
        return self.current_event

    def activate(self):
        self.activations.append("activate")

    def activateIgnoringOtherApps_(self, flag):
        if not isinstance(flag, bool):
            raise TypeError("activateIgnoringOtherApps: takes a BOOL")
        self.activations.append("activateIgnoringOtherApps:")


class _NSApplicationLegacy(_NSApplicationStub):
    """macOS 13 and earlier: no `activate`, so pyobjc has no such attribute."""

    activate = None

    def __getattribute__(self, name):
        if name == "activate":
            raise AttributeError("NSApplication has no attribute 'activate'")
        return super().__getattribute__(name)


class _NSEventClass(_ObjCObject):
    """AppKit's NSEvent class object, for the one class method the panel needs.

    (The *monitor* half of NSEvent is `_NSEventStub` further down, which
    `justpiano.hotkey` binds directly; keeping them apart is deliberate - the
    hotkey module caches its own reference and never imports AppKit here.)
    """

    @classmethod
    def mouseLocation(cls):
        return _AK.mouse_location


#: Knobs and recordings shared by the fake AppKit: what was painted, where the
#: mouse is, and whether this "Mac" has SF Symbols.
_AK = types.SimpleNamespace(
    painted=[],
    fill=None,
    stroke=None,
    mouse_location=_NSPoint(0.0, 0.0),
    sf_symbols=True,
    known_symbols={"gearshape.fill", "speaker.slash.fill", "speaker.wave.2.fill"},
)


def _ak_legacy_activation(legacy: bool) -> None:
    """Pretend to be macOS 13 (no `NSApplication.activate`), or 14+."""
    app = _NSApplicationStub.sharedApplication()
    app.__class__ = _NSApplicationLegacy if legacy else _NSApplicationStub


def _install_fake_appkit():
    """Put fake `AppKit`/`Foundation` modules in sys.modules and return a
    restore callable.

    `keyboardview` imports them inside `_build_classes()` and caches the result,
    so its cache is reset here as well - and put back afterwards, because the
    rest of the suite is about a machine with no pyobjc at all.
    """
    from justpiano import keyboardview

    ak = types.ModuleType("AppKit")
    ak.NSView = _NSViewStub
    ak.NSButton = _NSButtonStub
    ak.NSTextField = _NSTextFieldStub
    ak.NSViewController = _NSViewControllerStub
    ak.NSPopover = _NSPopoverStub
    ak.NSImage = _NSImage
    ak.NSColor = _NSColorStub
    ak.NSBezierPath = _NSBezierPathStub
    ak.NSFont = _NSFontStub
    ak.NSApplication = _NSApplicationStub
    ak.NSEvent = _NSEventClass
    ak.NSObject = _ObjCObject
    ak.NSPopoverBehaviorApplicationDefined = _NSPopoverStub.BEHAVIOR_APPLICATION_DEFINED
    ak.NSPopoverBehaviorTransient = _NSPopoverStub.BEHAVIOR_TRANSIENT
    ak.NSPopoverBehaviorSemitransient = _NSPopoverStub.BEHAVIOR_SEMITRANSIENT
    ak.NSRectEdgeMinX, ak.NSRectEdgeMinY = 0, 1
    ak.NSRectEdgeMaxX, ak.NSRectEdgeMaxY = 2, 3
    ak.NSMinYEdge = 1
    ak.NSEventTypeLeftMouseDown, ak.NSEventTypeLeftMouseUp = 1, 2
    ak.NSEventTypeRightMouseDown, ak.NSEventTypeRightMouseUp = 3, 4
    ak.NSEventTypeKeyDown, ak.NSEventTypeKeyUp = 10, 11
    ak.NSEventTypeFlagsChanged = 12
    ak.NSEventModifierFlagControl = 1 << 18
    ak.NSFontAttributeName = "NSFont"
    ak.NSForegroundColorAttributeName = "NSColor"
    ak.NSMakeRect, ak.NSMakePoint, ak.NSMakeSize = _NSMakeRect, _NSMakePoint, _NSMakeSize

    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _ObjCObject
    foundation.NSAttributedString = _NSAttributedStringStub
    foundation.NSMakeRect = _NSMakeRect
    foundation.NSMakePoint = _NSMakePoint
    foundation.NSMakeSize = _NSMakeSize

    saved_modules = {name: sys.modules.get(name)
                     for name in ("AppKit", "Foundation")}
    saved_state = (keyboardview._CLASSES, keyboardview._TRIED,
                   keyboardview._POPPING, keyboardview._POP_UP_QUEUED_AT)
    sys.modules["AppKit"] = ak
    sys.modules["Foundation"] = foundation
    keyboardview._CLASSES, keyboardview._TRIED = None, False
    keyboardview._POPPING, keyboardview._POP_UP_QUEUED_AT = False, None

    def restore():
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        (keyboardview._CLASSES, keyboardview._TRIED,
         keyboardview._POPPING, keyboardview._POP_UP_QUEUED_AT) = saved_state
        _RUNLOOP.clear()

    return ak, restore


# ============================================================== rumps stub ==
def _require_string_or_none(*objs):
    """rumps._internal.require_string_or_none -- the stub must reject what the
    real thing rejects, otherwise a non-string title only crashes on a Mac."""
    for obj in objs:
        if not (obj is None or isinstance(obj, str)):
            raise TypeError(f"a string or None is required but given {obj}, "
                            f"a {type(obj).__name__}")


def _require_string(*objs):
    """rumps._internal.require_string (_internal.py:14-20). `rumps.App.__init__`
    puts the name through it, and the name is what ends up in the menu bar when
    the status item has neither a title nor an image."""
    for obj in objs:
        if not isinstance(obj, str):
            raise TypeError(f"a string is required but given {obj}, "
                            f"a {type(obj).__name__}")


class _NSMenu:
    """AppKit's NSMenu, as far as rumps and Just Piano touch it: rumps adds an
    item per row (Menu.__setitem__, rumps.py:262-266) and calls removeAllItems()
    from Menu.clear().

    `items` is what makes a *row* visible to the tests as distinct from a dict
    entry: two rows sharing a title is precisely the state `tray.unique_labels`
    exists to prevent, and it is invisible in the dict.
    """

    def __init__(self):
        self.cleared = 0
        self.items = []

    def addItem_(self, item):
        self.items.append(item)

    def removeAllItems(self):
        self.cleared += 1
        self.items = []


def _nsimage_from_file(filename, dimensions=None, template=None):
    """rumps._nsimage_from_file (rumps.py:110-130).

    The file is opened *eagerly* -- twice: literally, then relative to the main
    script -- so a missing image raises out of `App.icon` instead of quietly
    drawing nothing. Every caller in the app has to survive that.
    """
    try:
        with open(filename):
            pass
    except IOError:
        main = sys.modules.get("__main__")
        base = os.path.dirname(getattr(main, "__file__", "") or "")
        filename = os.path.join(base, filename)
        with open(filename):        # still not there: the caller gets the error
            pass
    if dimensions is not None and len(tuple(dimensions)) != 2:
        # rumps hands `dimensions` straight to NSImage.setSize_, where pyobjc
        # rejects anything that is not an NSSize (rumps.py:126, and the 0.2.0
        # note that this "will no longer silently error").
        raise ValueError("dimensions must be a sequence of length 2")
    return _NSImage(filename, dimensions if dimensions is not None else (20, 20),
                    template)


class _MenuItem(dict):
    """Mirrors rumps.MenuItem: a dict of children that is also a clickable item.

    The constructor signature is rumps' exactly (rumps.py:434) -- no `**kwargs`
    catch-all: a keyword the real class does not take is a TypeError on a Mac,
    and a stub that swallows it hides the typo.
    """

    def __new__(cls, title="", *_args, **_kwargs):
        # rumps.MenuItem.__new__ (rumps.py:429-432): wrapping an existing
        # MenuItem hands the same instance back rather than making a second one.
        if isinstance(title, _MenuItem):
            return title
        return super().__new__(cls)

    def __init__(self, title="", callback=None, key=None, icon=None,
                 dimensions=None, template=None):
        if isinstance(title, _MenuItem):
            return                      # already initialised (see __new__)
        super().__init__()
        _require_string_or_none(key)
        self._title = str(title)
        self.callback = callback
        self.key = key
        self.state = 0
        self._sep_count = 0
        self._menu = None        # real rumps creates the submenu NSMenu lazily
        self._template = template
        self._icon = None
        self.set_icon(icon, dimensions, template)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        # rumps.MenuItem.title runs text_type(new_title) (rumps.py:462): unlike
        # App.title it *coerces* rather than raising, so the stub must coerce
        # too. Storing the raw object made this stub stricter than the real
        # library: `self.take_item.title = count` is harmless on a Mac but broke
        # every later "… in item.title" assertion here with a TypeError.
        self._title = str(value)

    @property
    def icon(self):
        return self._icon

    def set_icon(self, icon_path, dimensions=None, template=None):
        """rumps.MenuItem.set_icon (rumps.py:494-511): the image is built now, so
        a path that is not there raises out of the constructor."""
        self._icon_nsimage = (_nsimage_from_file(icon_path, dimensions, template)
                              if icon_path is not None else None)
        self._icon = icon_path

    def add(self, item):
        """rumps.Menu.add == `__setitem__(self._choose_key, item)`.

        The sentinel key is never already in the dict, so `addItem_` runs
        unconditionally (rumps.py:262-266) and a repeated title gives *two rows*
        with the dict entry bound to the later one, orphaning the earlier row.
        That is the behaviour `tray.unique_labels` exists to prevent, so the stub
        reproduces it instead of quietly dropping the duplicate -- which is what
        it used to do, making a colliding menu label look harmless here.
        """
        self._menu = self._menu or _NSMenu()
        if item is _SEPARATOR or item is None:
            self._sep_count += 1
            key = f"SeparatorMenuItem_{self._sep_count}"
        else:
            if not isinstance(item, _MenuItem):
                item = _MenuItem(item)
            key = item.title
        self._menu.addItem_(item)
        dict.__setitem__(self, key, item)

    def clear(self):
        if self._menu is None:
            # Exactly what rumps does: AttributeError on NoneType.removeAllItems
            raise AttributeError("'NoneType' object has no attribute 'removeAllItems'")
        self._menu.removeAllItems()
        super().clear()
        # rumps.Menu.clear() does *not* reset `_counts` (rumps.py:270-273), so a
        # rebuilt submenu keeps counting: its separator is SeparatorMenuItem_2.

    def set_callback(self, callback, key=None):
        _require_string_or_none(key)
        if key is not None:
            self.key = key
        self.callback = callback

    def click(self):
        if self.callback is None:
            raise AssertionError(f"menu item {self.title!r} is disabled")
        self.callback(self)


class _Menu(_MenuItem):
    """rumps.Menu -- the application's main menu. Unlike a MenuItem's submenu
    its NSMenu exists from construction (rumps.py:255), and that object is both
    what rumps hands to NSStatusItem.setMenu_ and what the panel's gear button
    has to be able to pop up again."""

    def __init__(self):
        super().__init__("__main__")
        self._menu = _NSMenu()



_SEPARATOR = object()


class _Timer:
    """rumps.Timer (rumps.py:658-736): an NSTimer on the run loop.

    `start()` on a running timer and `stop()` on a stopped one are both no-ops
    there, and the callback is handed the timer itself.
    """

    def __init__(self, callback, interval):
        if not callable(callback):
            raise TypeError("the timer callback must be callable")
        self.callback = callback
        self._interval = interval
        self.started = False

    @property
    def interval(self):
        return self._interval

    @interval.setter
    def interval(self, value):
        self._interval = value

    def set_callback(self, callback):
        if not callable(callback):
            raise TypeError("the timer callback must be callable")
        self.callback = callback

    def is_alive(self):
        return self.started

    def start(self):
        if not self.started:
            self.started = True

    def stop(self):
        if self.started:
            self.started = False

    def fire(self):
        # rumps' `callback_` swallows whatever the callback raises after printing
        # it (rumps.py:730-735). Deliberately *not* swallowed here: a tick that
        # blows up must be a FAIL rather than an invisible no-op.
        self.callback(self)


class _Response:
    """rumps.Response: `clicked` is 1 for the ok button and 0 for cancel."""

    def __init__(self, clicked, text=""):
        self._clicked = clicked
        self._text = text

    @property
    def clicked(self):
        return self._clicked

    @property
    def text(self):
        return self._text


class _Window:
    #: Which button the next `run()` reports. 1 = ok, 0 = cancel; tests set it
    #: so both branches of a confirmation dialog can be exercised.
    answer = 1

    #: Every window actually shown to the user, oldest first. Appended by `run()`
    #: rather than `__init__` so a dialog that is built but never run does not
    #: count as one the user saw.
    opened: list["_Window"] = []

    def __init__(self, message="", title="", default_text="", ok=None, cancel=None,
                 dimensions=(320, 160), secure=False):
        # rumps.Window.__init__ (rumps.py:763-787): `ok` must be a string or
        # None, `dimensions` a length-2 sequence (it goes into NSMakeRect), and
        # there is no **kwargs -- an unexpected keyword is a TypeError on a Mac.
        _require_string_or_none(ok)
        if len(tuple(dimensions)) != 2:
            raise ValueError("dimensions must be a sequence of length 2")
        self.message = str(message)
        self.title = str(title)
        self.ok = ok
        self.cancel = cancel
        self._cancel = bool(cancel)
        self.secure = bool(secure)
        self.default_text = str(default_text)

    def run(self):
        _Window.opened.append(self)
        return _Response(_Window.answer, self.default_text)


class _NSStatusItemButton(_NSControlStub):
    """NSStatusBarButton: the control rumps' status item hangs off, and the only
    place a click can be intercepted once the menu is taken off the item.

    An `NSButton` subclass, so it is a real view as far as the fake runtime is
    concerned: `showRelativeToRect:ofView:preferredEdge:` can be anchored to it,
    and its `window()` is the status bar window whose frame *is* the icon's
    rectangle on screen - which is what `keyboardview._click_landed_on` measures
    a mouse-down against.
    """

    #: Where the icon sits on screen, in the coordinates NSEvent.mouseLocation
    #: reports (origin bottom-left of the main display).
    FRAME = (1180.0, 1050.0, 26.0, 22.0)

    def __init__(self):
        self._init_control(_NSRect(0.0, 0.0, self.FRAME[2], self.FRAME[3]))
        self._window = _NSWindow(_NSRect(*self.FRAME))


class _NSStatusItem:
    """NSStatusItem, as far as rumps sets it up and Just Piano re-wires it."""

    def __init__(self):
        self._menu = None
        self._title = None
        self._image = None
        self._button = _NSStatusItemButton()
        self.highlight = False
        #: Every menu that was popped up on demand, in order.
        self.popped = []
        #: Every title the item was ever given, so a one-frame flash (rumps'
        #: fallback-on-name, say) can be caught after the fact.
        self.titles = []
        #: Called from inside popUpStatusItemMenu_, i.e. from the modal tracking
        #: loop AppKit runs there. Failure injection for the re-entrancy guard.
        self.while_popping = None
        #: Failure injection: popUpStatusItemMenu: is soft-deprecated and may go.
        self.fail_pop_up = False

    def setHighlightMode_(self, value):
        self.highlight = bool(value)

    def setTitle_(self, title):
        # pyobjc: setTitle_ takes an NSString or nil. rumps hands it App._title,
        # which its own setter has already restricted to a string or None.
        _require_string_or_none(title)
        self._title = title
        self.titles.append(title)

    def setImage_(self, image):
        if not (image is None or isinstance(image, _NSImage)):
            raise TypeError("setImage: requires an NSImage or nil, not "
                            f"{type(image).__name__}")
        self._image = image

    def title(self):
        return self._title

    def image(self):
        return self._image

    def button(self):
        return self._button

    def menu(self):
        return self._menu

    def setMenu_(self, menu):
        if not (menu is None or isinstance(menu, _NSMenu)):
            raise TypeError("setMenu: requires an NSMenu or nil, not "
                            f"{type(menu).__name__}")
        self._menu = menu

    def popUpStatusItemMenu_(self, menu):
        # ObjC throws NSInvalidArgumentException for a nil menu here.
        if not isinstance(menu, _NSMenu):
            raise ValueError("popUpStatusItemMenu: requires an NSMenu")
        if self.fail_pop_up:
            raise _ObjCError("-[NSStatusItem popUpStatusItemMenu:] is unavailable")
        self.popped.append(menu)
        # Everything inside here runs from a modal tracking loop of AppKit's own,
        # which is exactly where a second pop-up must not be able to start.
        hook, self.while_popping = self.while_popping, None
        if hook is not None:
            hook()


class _NSApp:
    """rumps' NSApplication delegate (rumps.NSApp), reduced to the parts the app
    reads: `nsstatusitem`, created by initializeStatusBar()."""

    def __init__(self, app):
        self._app = app.__dict__            # rumps.py:1187
        self.nsstatusitem = None

    def initializeStatusBar(self):
        """Mirrors rumps.NSApp.initializeStatusBar (rumps.py:939-954): make the
        status item, show the icon and the title, append the quit button if there
        is one and hand the whole main menu to the item."""
        self.nsstatusitem = _NSStatusItem()
        self.nsstatusitem.setHighlightMode_(True)
        self.setStatusBarIcon()
        self.setStatusBarTitle()
        quit_button = self._app["_quit_button"]
        if quit_button is not None:
            quit_button.set_callback(_quit_application)
            self._app["_menu"].add(quit_button)
        self.nsstatusitem.setMenu_(self._app["_menu"]._menu)

    def setStatusBarTitle(self):
        self.nsstatusitem.setTitle_(self._app["_title"])
        self.fallbackOnName()

    def setStatusBarIcon(self):
        self.nsstatusitem.setImage_(self._app["_icon_nsimage"])
        self.fallbackOnName()

    def fallbackOnName(self):
        """rumps.py:964-966. An item with neither a title nor an image shows the
        application *name* instead - which is why the order in which a title and
        an icon are swapped is visible to the user."""
        if not (self.nsstatusitem.title() or self.nsstatusitem.image()):
            self.nsstatusitem.setTitle_(self._app["_name"])


class _EventEmitter:
    """rumps.events.EventEmitter: a set of callbacks, each invoked through
    `_internal.call_as_function_or_method` (which passes a bound method straight
    through), with every exception swallowed after a traceback.

    Swallowing is faithful - and is also exactly how a before_start hook that
    raises would pass unnoticed - so the exceptions are kept for the tests.
    """

    def __init__(self, name):
        self.name = name
        self.callbacks = set()
        self.errors = []

    def register(self, func):
        self.callbacks.add(func)
        return func

    def unregister(self, func):
        try:
            self.callbacks.remove(func)
            return True
        except KeyError:
            return False

    def emit(self, *args, **kwargs):
        for callback in list(self.callbacks):
            try:
                callback(*args, **kwargs)
            except Exception as exc:
                self.errors.append(exc)

    __call__ = register


def _quit_application(sender=None):
    _QUIT_CALLS.append(sender)


_QUIT_CALLS: list = []


class _App:
    def __init__(self, name, title=None, icon=None, template=None, menu=None,
                 quit_button="Quit"):
        self._name = name
        self._icon = self._icon_nsimage = self._title = None
        self._template = template
        self.icon = icon
        self.title = title
        # rumps.App.quit_button: None means "no default quit item is added at
        # launch", so some other item must call quit_application.
        self._quit_button = None if quit_button is None else _MenuItem(quit_button)
        self._menu = _Menu()

    @property
    def name(self):
        return self._name

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        # rumps.App.title runs require_string_or_none: assigning e.g. an int
        # raises TypeError on a real Mac, so the stub must too.
        _require_string_or_none(value)
        self._title = value
        try:
            self._nsapp.setStatusBarTitle()
        except AttributeError:      # before run(): there is no status item yet
            pass

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, icon_path):
        # rumps.App.icon (rumps.py:1087-1095) builds the NSImage *now*, so a path
        # that is not there raises out of the assignment -- and the template flag
        # comes from the App, which is what makes the image follow the menu bar's
        # light/dark appearance.
        new_icon = (_nsimage_from_file(icon_path, template=self._template)
                    if icon_path is not None else None)
        self._icon = icon_path
        self._icon_nsimage = new_icon
        try:
            self._nsapp.setStatusBarIcon()
        except AttributeError:
            pass

    @property
    def template(self):
        return self._template

    @template.setter
    def template(self, template_mode):
        self._template = template_mode
        self.icon = self._icon          # rumps re-loads to apply the flag

    @property
    def menu(self):
        return self._menu

    @menu.setter
    def menu(self, items):
        for item in items:
            self._menu.add(item)

    def run(self):
        raise AssertionError("run() should not be called in the smoke test")

    def _rumps_run_setup(self):
        """The part of rumps.App.run() that happens before the event loop
        (rumps.py:1176-1195): build the NSApp delegate and the status bar item.
        run() itself never returns, so the suite drives this by hand -- and then
        emits before_start itself, as run() does immediately afterwards.
        """
        self._nsapp = _NSApp(self)
        self._nsapp.initializeStatusBar()
        return self._nsapp


def _install_rumps():
    rumps = types.ModuleType("rumps")
    rumps.MenuItem = _MenuItem
    rumps.Menu = _Menu
    rumps.App = _App
    rumps.Timer = _Timer
    rumps.Window = _Window
    rumps.Response = _Response
    rumps.separator = _SEPARATOR
    rumps.alerts = []
    rumps.notifications = []
    rumps.alert = lambda title="", message="", ok=None, **kw: rumps.alerts.append((title, message))
    rumps.notification = lambda title, subtitle, message, **kw: rumps.notifications.append(
        (title, subtitle, message))
    rumps.quit_application = _quit_application
    events = types.ModuleType("rumps.events")
    events.EventEmitter = _EventEmitter
    for name in ("before_start", "on_notification", "on_sleep", "on_wake",
                 "before_quit"):
        setattr(events, name, _EventEmitter(name))
    rumps.events = events
    sys.modules["rumps.events"] = events
    sys.modules["rumps"] = rumps
    return rumps



# ======================================================== sounddevice stub ==
#: What PortAudio adds to a block period, so the latency the app reports is a
#: number of the *stream's* own and not one the blocksize could produce by
#: accident. 256 frames at 44.1 kHz is 5.8 ms, which rounds to the same "6 ms"
#: as `AudioEngine.latency_ms`' blocksize fallback -- so with the old hardcoded
#: 0.006 s here, a `latency_ms` that ignored the stream entirely (or returned a
#: constant) was indistinguishable from a correct one.
_STREAM_LATENCY_OVERHEAD = 0.0075


class _FakeStream:
    """Drives the audio callback from a worker thread, like PortAudio would."""

    def __init__(self, samplerate, blocksize, channels, dtype, device, latency,
                 callback):
        # sounddevice takes the *requested* latency ('low', 'high' or seconds)
        # and publishes the one it actually got on `.latency`; a bad value is a
        # PortAudioError, not a silently ignored keyword.
        if latency not in ("low", "high") and not isinstance(latency, (int, float)):
            raise _PortAudioError(f"Invalid latency setting {latency!r}")
        if dtype not in ("float32", "int16", "int32", "int8", "uint8"):
            raise _PortAudioError(f"Invalid dtype {dtype!r}")
        if isinstance(channels, bool) or not isinstance(channels, int) or channels < 1:
            raise _PortAudioError(f"Invalid number of channels {channels!r}")
        self.samplerate = samplerate
        self.blocksize = blocksize or 256
        self.channels = channels
        self.dtype = dtype
        self.device = device        # the app has to be able to observe this
        self.requested_latency = latency
        self.callback = callback
        self.latency = self.blocksize / self.samplerate + _STREAM_LATENCY_OVERHEAD
        self.captured: list[np.ndarray] = []
        self.frames = 0
        self.closed = False
        self._stop = threading.Event()
        self._thread = None

    @property
    def active(self):
        return self._thread is not None and not self._stop.is_set()

    def start(self):
        self._require_open()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _require_open(self):
        # sounddevice raises for any operation on a closed stream; a stub that
        # keeps working would hide a use-after-close in the engine.
        if self.closed:
            raise _PortAudioError("Error: PortAudio stream is closed "
                                  "[PaErrorCode -9988]")

    def _run(self):
        period = self.blocksize / self.samplerate
        next_at = time.monotonic()
        while not self._stop.is_set():
            out = np.zeros((self.blocksize, self.channels), dtype=np.float32)
            self.callback(out, self.blocksize, None, None)
            self.captured.append(out)
            self.frames += self.blocksize
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()

    def stop(self):
        self._require_open()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def close(self):
        # close() on an already closed stream is a no-op in sounddevice, and it
        # stops the stream first (PortAudio joins the callback thread).
        if self.closed:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.closed = True


_STREAMS: list[_FakeStream] = []

_DEVICES = [
    {"name": "Fake Speakers", "max_output_channels": 2, "max_input_channels": 0,
     "default_samplerate": 44100.0, "hostapi": 0},
    {"name": "Fake Interface", "max_output_channels": 2, "max_input_channels": 2,
     "default_samplerate": 48000.0, "hostapi": 0},
    {"name": "Fake Mic", "max_output_channels": 0, "max_input_channels": 1,
     "default_samplerate": 44100.0, "hostapi": 0},
]

# Failure injection: PortAudio really does refuse a device that vanished between
# query_devices() and OutputStream(), and AudioEngine.start()'s except branch
# plus _start_audio()'s fallback are only reachable when the stub can fail.
_SD = types.SimpleNamespace(fail_devices=set(), attempts=[])


class _PortAudioError(Exception):
    pass


def _install_sounddevice():
    sd = types.ModuleType("sounddevice")

    def output_stream(**kwargs):
        device = kwargs.get("device")
        _SD.attempts.append(device)
        if device in _SD.fail_devices:
            raise _PortAudioError(
                f"Error opening OutputStream: Invalid device [PaErrorCode -9996] "
                f"(device {device!r})")
        # PortAudio checks the device index and its channel count before it opens
        # anything: an index that is not there, or an output device with fewer
        # channels than asked for, is an error rather than a silent mono stream.
        if device is not None:
            if (isinstance(device, bool) or not isinstance(device, int)
                    or not 0 <= device < len(_DEVICES)):
                raise _PortAudioError(f"Error querying device {device!r}")
            if _DEVICES[device]["max_output_channels"] < kwargs.get("channels", 2):
                raise _PortAudioError(
                    "Error opening OutputStream: Invalid number of channels "
                    "[PaErrorCode -9998]")
        stream = _FakeStream(**kwargs)
        _STREAMS.append(stream)
        return stream

    def query_devices(idx=None, kind=None):
        if idx is None:
            return [dict(dev, index=i) for i, dev in enumerate(_DEVICES)]
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise _PortAudioError(f"error querying device {idx!r}")
        if not 0 <= idx < len(_DEVICES):
            raise _PortAudioError(f"error querying device {idx}")
        return dict(_DEVICES[idx], index=idx)

    sd.OutputStream = output_stream
    sd.query_devices = query_devices
    sd.PortAudioError = _PortAudioError
    # sounddevice really does export these, and `synth` only ever catches
    # Exception - but a stub that is missing a name the app might grow into is a
    # stub that says "no such attribute" where a Mac says something else.
    sd.CallbackStop = type("CallbackStop", (Exception,), {})
    sd.CallbackAbort = type("CallbackAbort", (Exception,), {})
    sd.default = types.SimpleNamespace(device=(0, 0), samplerate=None,
                                       latency=("high", "high"))
    sys.modules["sounddevice"] = sd


# ============================================================= rtmidi stub ==
# python-rtmidi's own exception tree (_rtmidi.pyx:270-300): the base is
# RtMidiError, and each subclass also inherits the Python exception a caller
# would expect. `midi_in` only ever catches Exception, but a stub that raises
# IndexError where the library raises InvalidPortError is a stub that cannot show
# what a real refusal looks like -- and one that is missing InvalidPortError
# altogether cannot even be asked.
class _RtMidiError(Exception):
    """rtmidi.RtMidiError: the base of everything the library raises."""


class _RtMidiSystemError(_RtMidiError, OSError):
    """rtmidi.SystemError: a driver/system failure - a port held elsewhere."""


class _RtMidiInvalidPortError(_RtMidiError, ValueError):
    """rtmidi.InvalidPortError: no such port number."""


class _RtMidiInvalidUseError(_RtMidiError, RuntimeError):
    """rtmidi.InvalidUseError: a second port on one MidiIn/MidiOut instance."""


class _RtMidiMemoryAllocationError(_RtMidiError, MemoryError):
    pass


class _RtMidiNoDevicesError(_RtMidiError, ValueError):
    pass


class _RtMidiUnsupportedOperationError(_RtMidiError, RuntimeError):
    pass


class _FakeMidiBase:
    """The half of rtmidi.MidiBase both directions share.

    Faithful in the four places `midi_in.py` can trip over on a Mac:

    * one port per instance -- `open_port`/`open_virtual_port` on an instance
      that already opened one is `InvalidUseError` (`_check_port`, .pyx:472-481);
    * `open_port` takes an `unsigned int`, so an out-of-range number is
      `InvalidPortError` and a negative one an OverflowError from the cast;
    * `close_port()` does *not* close a virtual port (.pyx:647-658) -- only
      deleting the instance does, which is why `shutdown()` deletes it;
    * `delete()` makes the instance unusable: touching it again segfaults the
      real library, so here it raises rather than quietly working.
    """

    #: The ports every instance sees, and which of them refuse to open.
    ports: list = []
    fail_ports: set = set()
    #: ("open"|"close", port name) in call order, so the tests can assert that
    #: the replacement port is opened *before* the old one is surrendered.
    events: list = []
    live: list = []

    def __init__(self):
        self._port = None           # None, an index, or -1 for a virtual port
        self._deleted = False
        self._callback = None
        type(self).live.append(self)

    # ---------------------------------------------------------------- shared
    def _check_deleted(self):
        if self._deleted:
            raise RuntimeError(f"{type(self).__name__} instance used after "
                               "delete() -- a real one would segfault here")

    def _check_port(self):
        self._check_deleted()
        if self._port == -1:
            raise _RtMidiInvalidUseError(f"{self!r} already opened virtual port.")
        if self._port is not None:
            raise _RtMidiInvalidUseError(f"{self!r} already opened port "
                                         f"{self._port}.")

    def get_ports(self, encoding="auto"):
        self._check_deleted()
        return list(type(self).ports)

    def get_port_count(self):
        self._check_deleted()
        return len(type(self).ports)

    def get_port_name(self, port, encoding="auto"):
        self._check_deleted()
        if port >= len(type(self).ports):
            raise _RtMidiInvalidPortError(f"Invalid port number: {port}")
        return type(self).ports[port]

    def is_port_open(self):
        return self._port is not None

    def open_port(self, port=0, name=None):
        self._check_port()
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("an integer is required (got type "
                            f"{type(port).__name__})")
        if port < 0:
            raise OverflowError("can't convert negative value to unsigned int")
        if port >= len(type(self).ports):
            raise _RtMidiInvalidPortError(
                f"'port' argument (= {port}) is invalid.")
        port_name = type(self).ports[port]
        if port_name in type(self).fail_ports:
            # A CoreMIDI source another client holds exclusively: rtmidi surfaces
            # the driver's own message through its error callback as SystemError.
            raise _RtMidiSystemError(
                f"MidiInCore::openPort: error creating OS-X MIDI port "
                f"({port_name})")
        self._port = port
        self._on_open(port, port_name)
        return self

    def open_virtual_port(self, name=None):
        self._check_port()
        _require_string_or_none(name)
        self._port = -1
        self._on_open_virtual(name)
        return self

    def close_port(self):
        self._check_deleted()
        self.cancel_callback()
        if self._port != -1:
            if self._port is not None:
                self._on_close(type(self).ports[self._port]
                               if self._port < len(type(self).ports) else None)
            self._port = None

    def cancel_callback(self):
        self._check_deleted()
        self._callback = None

    def delete(self):
        if not self._deleted:
            self._deleted = True
            self._callback = None
            self._port = None
            if self in type(self).live:
                type(self).live.remove(self)

    @property
    def is_deleted(self):
        return self._deleted

    # -------------------------------------------------------------- subclass
    def _on_open(self, index, name):
        pass

    def _on_open_virtual(self, name):
        pass

    def _on_close(self, name):
        pass


class _FakeMidiIn(_FakeMidiBase):
    ports = ["Fake Studio Keys 88", "IAC Driver Bus 1"]
    live: list["_FakeMidiIn"] = []
    last_index = None
    #: Ports whose open_port() is refused, like a keyboard already claimed by
    #: another MIDI client. Failure injection is the only way to reach
    #: MidiInput's error/retry paths from here.
    fail_ports: set[str] = set()
    events: list[tuple[str, str]] = []

    def __init__(self):
        super().__init__()
        self.port = None            # the *name* of the open hardware port
        self.virtual = None

    def _on_open(self, index, name):
        self.port = name
        _FakeMidiIn.last_index = index
        _FakeMidiIn.events.append(("open", name))

    def _on_open_virtual(self, name):
        self.virtual = name

    def _on_close(self, name):
        if self.port is not None:
            _FakeMidiIn.events.append(("close", self.port))
        self.port = None

    def ignore_types(self, sysex=True, timing=True, active_sense=True):
        self._check_deleted()
        for flag in (sysex, timing, active_sense):
            if not isinstance(flag, bool):
                raise TypeError("ignore_types takes booleans")

    def set_callback(self, callback, data=None):
        # The real library stores whatever it is given and only fails when a
        # message arrives on the C thread; failing here is stricter, which is the
        # direction a stub is allowed to differ in.
        self._check_deleted()
        if not callable(callback):
            raise TypeError("the MIDI callback must be callable")
        self._callback = (callback, data)

    @property
    def callback(self):
        return None if self._callback is None else self._callback[0]

    @classmethod
    def send(cls, status, d1, d2):
        for inst in list(cls.live):
            if inst.port and inst.callback:
                inst.callback(([status, d1, d2], 0.0))
                return True
        return False


class _FakeMidiOut(_FakeMidiBase):
    """rtmidi.MidiOut. Just Piano never sends MIDI, but the module exports it and
    a stub that omits it answers AttributeError where a Mac would work."""

    ports: list = []
    live: list = []
    fail_ports: set = set()
    events: list = []

    def send_message(self, message):
        self._check_deleted()
        if self._port is None:
            raise _RtMidiInvalidUseError("No port opened.")
        if not message:
            raise ValueError("message must not be empty")


def _install_rtmidi():
    rtmidi = types.ModuleType("rtmidi")
    rtmidi.MidiIn = _FakeMidiIn
    rtmidi.MidiOut = _FakeMidiOut
    rtmidi.RtMidiError = _RtMidiError
    rtmidi.SystemError = _RtMidiSystemError
    rtmidi.InvalidPortError = _RtMidiInvalidPortError
    rtmidi.InvalidUseError = _RtMidiInvalidUseError
    rtmidi.MemoryAllocationError = _RtMidiMemoryAllocationError
    rtmidi.NoDevicesError = _RtMidiNoDevicesError
    rtmidi.UnsupportedOperationError = _RtMidiUnsupportedOperationError
    rtmidi.API_UNSPECIFIED, rtmidi.API_MACOSX_CORE = 0, 1
    rtmidi.API_LINUX_ALSA, rtmidi.API_UNIX_JACK = 2, 3
    rtmidi.API_WINDOWS_MM, rtmidi.API_RTMIDI_DUMMY = 4, 5
    rtmidi.get_compiled_api = lambda: [rtmidi.API_MACOSX_CORE]
    rtmidi.get_api_name = lambda api: "coremidi"
    rtmidi.get_api_display_name = lambda api: "CoreMIDI"
    rtmidi.get_rtmidi_version = lambda: "6.0.0"
    rtmidi.version = types.SimpleNamespace(version="1.5.8")
    sys.modules["rtmidi"] = rtmidi


# ==================================================== keyboard panel stubs ==
class _FakePanel:
    """Stand-in for keyboardview.KeyboardPanel, i.e. for the NSPopover.

    Mirrors the whole protocol the tray uses -- `is_open`, `open()`, `close()`,
    `redraw()`, `refresh_mute()`, `dismissed_at` and `forget_dismissal()` -- and
    is no more forgiving than AppKit: redrawing a panel that is not on screen is
    a bug (it would mark a view outside the window hierarchy), and `open()` can
    fail, exactly as `showRelativeToRect:ofView:preferredEdge:` does when the
    status item button has no window yet.

    `dismissed_at` is the timestamp the real panel's `popoverDidClose:` delegate
    writes when the *user* dismissed the popover, and it is the difference
    between the two questions the tray has to tell apart on a click (see
    `tray._panel_was_open`). It is stamped by `dismiss()` alone: a `close()` this
    code asked for says nothing about what the user wanted, and stamping it would
    swallow the next click on the icon.
    """

    def __init__(self, controller):
        self.controller = controller
        self.is_open = False
        self.opens = 0
        self.closes = 0
        self.redraws = 0
        self.mute_refreshes = 0
        #: `time.monotonic()` of the last dismissal the user caused, or None.
        self.dismissed_at = None
        self.forgotten = 0
        #: Failure injection: a popover that refuses to appear.
        self.fail_open = False
        # The real panel paints its mute button from the controller as the last
        # thing it does in __init__, so a panel built while muted is already
        # right (keyboardview.py: KeyboardPanel.__init__).
        self.refresh_mute()

    def open(self):
        if self.fail_open:
            return False
        self.opens += 1
        self.is_open = True
        self.dismissed_at = None    # whatever closed it last is history
        return True

    def close(self):
        if self.is_open:
            self.closes += 1
        self.is_open = False
        # A close of ours is not a dismissal: `KeyboardPanel._note_closed`
        # clears the stamp for exactly this case.
        self.dismissed_at = None

    def redraw(self):
        if not self.is_open:
            raise AssertionError("redraw() while the panel is closed")
        self.redraws += 1

    def refresh_mute(self):
        """What the button is showing, read off the controller like the real one:
        the panel never keeps a mute state of its own."""
        self.muted = bool(self.controller.muted)
        self.mute_refreshes += 1

    def dismiss(self, when=None, on_icon=True):
        """The transient popover closed itself on a mouse-*down* somewhere else.

        When that somewhere else is the menu bar icon (`on_icon`), the click is
        still coming: it arrives as the status item's action on mouse-up, which is
        why the timestamp is here for the tray to consume. A dismissal caused by
        a click anywhere else leaves no stamp, because no action is on its way -
        which is what `keyboardview._click_landed_on` decides on a real Mac.
        """
        was_open = self.is_open
        self.is_open = False
        if was_open:
            self.closes += 1
            if on_icon:
                self.dismissed_at = time.monotonic() if when is None else when

    def forget_dismissal(self):
        self.dismissed_at = None
        self.forgotten += 1


class _FakeClickHandler:
    """Stand-in for the ObjC target keyboardview installs on the status item
    button. Mirrors its single selector; the Mac-only part is reading
    NSApp.currentEvent() to tell a right click from a left one, which is what
    `secondary` stands in for here."""

    def __init__(self, on_click):
        self.on_click = on_click
        self.secondary = False

    def statusItemClicked_(self, sender):
        self.on_click(self.secondary)


# ================================================== NSEvent monitor stubs ==
class _KeyEvent:
    """NSEvent, as far as a key-down monitor reads one."""

    KEY_DOWN = 10                       # NSEventTypeKeyDown

    def __init__(self, key_code, flags=0, repeat=False):
        self._key_code = key_code
        self._flags = flags
        self._repeat = repeat

    def type(self):
        return self.KEY_DOWN

    def keyCode(self):
        return self._key_code

    def modifierFlags(self):
        return self._flags

    def isARepeat(self):
        return self._repeat


class _NSEventStub:
    """AppKit's NSEvent, restricted to what `justpiano.hotkey` uses.

    As strict as the real API in the places that bite:

    * the mask is an `NSEventMask`, i.e. an integer, and the handler is a block;
    * the factories hand back a *token* that the caller has to keep, and
      `removeMonitor:` accepts each one exactly once -- an unknown object throws
      in ObjC, so a double teardown is a bug here too;
    * a **global** monitor never sees events that go to this application, and a
      **local** monitor never sees anybody else's: that split is the whole reason
      the app installs both, so `post()` honours it;
    * a local handler's return value goes back to AppKit -- the event to pass it
      on, `None` to swallow it -- and anything else is meaningless to ObjC.
    """

    def __init__(self):
        #: token -> ("local"|"global", mask, handler)
        self.tokens: dict = {}
        self.installed: list = []       # every install, in order
        self.removed: list = []
        #: Failure injection: `addGlobalMonitor...` returning nil.
        self.fail_global = False
        self._next = 0

    def _add(self, kind, mask, handler):
        if isinstance(mask, bool) or not isinstance(mask, int):
            raise TypeError("NSEventMask must be an integer")
        if not callable(handler):
            raise TypeError("the event handler must be a block")
        self.installed.append(kind)
        if kind == "global" and self.fail_global:
            return None
        self._next += 1
        token = f"<NSEventMonitor {kind} {self._next}>"
        self.tokens[token] = (kind, mask, handler)
        return token

    def addLocalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        return self._add("local", mask, handler)

    def addGlobalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        return self._add("global", mask, handler)

    def removeMonitor_(self, token):
        if token not in self.tokens:
            raise ValueError(f"removeMonitor: unknown event monitor {token!r}")
        del self.tokens[token]
        self.removed.append(token)

    # ------------------------------------------------------------ driving it
    def post(self, event, scope="global"):
        """Deliver a key press. `scope` is who has focus: "local" is Just Piano
        itself, "global" is any other app. Returns the monitors that saw it."""
        seen = 0
        for kind, mask, handler in list(self.tokens.values()):
            if not mask & (1 << event.type()) or kind != scope:
                continue
            result = handler(event)
            if kind == "local" and result is not None and result is not event:
                raise AssertionError("a local handler must return the event it "
                                     "was given, or None to swallow it")
            seen += 1
        return seen

    @property
    def kinds(self):
        return sorted(kind for kind, _mask, _handler in self.tokens.values())


def _install_nsevent(hotkey, trusted=None):
    """Point `justpiano.hotkey` at the NSEvent stub, as if pyobjc were here.

    `hotkey` binds NSEvent and `AXIsProcessTrusted` lazily and caches both, so
    the caches are the seam - faking an importable `AppKit` package instead would
    make `keyboardview`'s "there is no pyobjc on this machine" checks lie.
    `trusted` is what Accessibility answers: True, False, or None for "the symbol
    could not be bound at all". Returns (stub, restore).
    """
    stub = _NSEventStub()
    saved = (hotkey._NSEVENT, hotkey._NSEVENT_TRIED,
             hotkey._AX_TRUSTED, hotkey._AX_TRIED)
    hotkey._NSEVENT, hotkey._NSEVENT_TRIED = stub, True
    _set_trusted(hotkey, trusted)

    def restore():
        (hotkey._NSEVENT, hotkey._NSEVENT_TRIED,
         hotkey._AX_TRUSTED, hotkey._AX_TRIED) = saved

    return stub, restore


def _set_trusted(hotkey, trusted):
    """What `AXIsProcessTrusted()` reports from now on (None = unbindable)."""
    hotkey._AX_TRIED = True
    hotkey._AX_TRUSTED = None if trusted is None else (lambda: trusted)



# =================================================================== test ==
NOTE_ON, NOTE_OFF, CC = 0x90, 0x80, 0xB0

BANK_BUILD_TIMEOUT = 90.0    # cold build of 88 keys x 2 layers; selftest allows 60 s


def wait_rendered(stream, seconds, timeout=45.0):
    """Wait until the audio stub has actually rendered `seconds` of audio.

    Voices retire after a fixed amount of *rendered* audio, not wall clock: the
    stub drops blocks rather than catching up when it falls behind, so envelope
    assertions have to be tied to the stream's own progress.
    """
    target = stream.frames + int(seconds * stream.samplerate)
    return wait_until(lambda: stream.frames >= target, timeout)


def rms(block):
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


def main() -> int:
    print("Just Piano tray smoke test")
    rumps = _install_rumps()
    _install_sounddevice()
    _install_rtmidi()

    tmpdir = tempfile.mkdtemp(prefix="justpiano-smoke-")
    _atexit.register(_shutil.rmtree, tmpdir, ignore_errors=True)
    from justpiano import macui, recorder as rec
    saved_paths = []

    def fake_save_panel(default_name, directory, file_type="mid", prompt="Save"):
        path = os.path.join(tmpdir, default_name)
        saved_paths.append(path)
        return path

    macui.save_panel = fake_save_panel
    macui.reveal_in_finder = lambda path: None
    macui.open_path = lambda path: None
    macui.bundle_path = lambda: None
    macui.notify = lambda *a, **k: rumps.notifications.append(a)
    macui.alert = lambda title, message, ok="OK": rumps.alerts.append((title, message))

    from justpiano.config import DEFAULTS, SETTINGS_PATH, Settings
    from justpiano.tray import (
        ICON_IDLE, ICON_REC, VOICING_MENU_TITLE, JustPianoApp, unique_labels,
    )

    print("\n[1] startup")
    app = JustPianoApp()
    # `app.title is not None` was really `ICON_IDLE is not None`: assert the
    # startup title contract instead.
    check("menu bar item shows the idle icon", app.title == ICON_IDLE, repr(app.title))
    check("audio stream opened on the default device",
          bool(_STREAMS) and app._audio_started and _STREAMS[-1].device is None,
          f"device={_STREAMS[-1].device if _STREAMS else 'none'}")
    check("virtual MIDI port published",
          any(m.virtual == "Just Piano" for m in _FakeMidiIn.live))
    check("hardware keyboard auto-selected (not the IAC loopback)",
          app.midi.port_name == "Fake Studio Keys 88", str(app.midi.port_name))

    check("sample bank finished loading",
          wait_until(lambda: app.bank.ready, BANK_BUILD_TIMEOUT, 0.1),
          f"progress={app.bank.progress:.0%}")

    app.timer.fire()
    check("status line reports the connected keyboard",
          "Fake Studio Keys 88" in app.status_item.title, app.status_item.title)
    check("the menu bar falls back to the idle icon when nothing is playing",
          app.title == ICON_IDLE, repr(app.title))

    # The "still building" UI is only reachable while the bank renders, so drive
    # it deliberately. rumps.App.title raises TypeError on a non-string, so a
    # title built from a raw number would abort the timer on a real Mac.
    app.bank.ready, app.bank.progress = False, 0.42
    raised = None
    try:
        app.timer.fire()
    except Exception as exc:
        raised = exc
    check("the build-progress title is a string rumps accepts",
          raised is None and app.title == f"{ICON_IDLE} 42%",
          f"{raised!r} {app.title!r}")
    check("the status line reports build progress",
          "Building piano" in app.status_item.title and "42%" in app.status_item.title,
          app.status_item.title)
    # A render failure leaves SampleBank.error set and `ready` False forever, so
    # the status line must say what happened instead of freezing on a percentage
    # the user will wait on for the rest of the session.
    app.bank.error = "MemoryError: cannot allocate 2.1 GiB"
    app.timer.fire()
    check("a failed sample-bank build is reported instead of a frozen percentage",
          app.status_item.title.startswith("⚠️")
          and "MemoryError" in app.status_item.title, app.status_item.title)
    app.bank.error = None
    app.bank.ready, app.bank.progress = True, 1.0
    app.timer.fire()

    print("\n[2] playing")
    stream = _STREAMS[0]
    stream.captured.clear()
    # Deep bass keys on purpose: their rendered buffers are 5.7-8.0 s long, so a
    # voice that disappears during the pedal checks below can only have been
    # released -- it cannot have run out of samples.
    chord = (21, 24, 28, 33)
    delivered = True
    for note in chord:
        # send() returns False when nothing is listening; discarding that return
        # value is how a broken MIDI path turns into a silent pass.
        delivered = _FakeMidiIn.send(NOTE_ON, note, 96) and delivered
        time.sleep(0.05)
    check("every MIDI message was delivered to the app", delivered)
    wait_rendered(stream, 0.2)
    audio = np.concatenate(stream.captured) if stream.captured else np.zeros((1, 2))
    stream.captured.clear()
    level = rms(audio)
    peak = float(np.abs(audio).max())
    check("audio callback produced sound", level > 1e-3, f"rms={level:.4f}")
    # `<= 1.0` cannot fail (render() ends in tanh()*0.92). Assert the band a
    # four-note chord has to land in, which a mix-gain regression leaves.
    check("callback output is finite and not driven into the limiter",
          np.isfinite(audio).all() and 0.05 < peak < 0.80, f"peak={peak:.3f}")
    check("voices are active", app.engine.active_voices == 4,
          f"{app.engine.active_voices}")

    _FakeMidiIn.send(CC, 64, 127)
    for note in chord:
        _FakeMidiIn.send(NOTE_OFF, note, 0)
    # Un-pedalled, a 160 ms release retires the voices after ~1.4 s of rendered
    # audio; asserting immediately after the note-offs proved nothing at all.
    check("the release window really elapsed", wait_rendered(stream, 1.6),
          f"{stream.frames / stream.samplerate:.1f}s rendered")
    check("sustain pedal holds the notes", app.engine.active_voices == 4,
          f"{app.engine.active_voices}")

    _FakeMidiIn.send(CC, 64, 0)
    at_pedal_up = stream.frames
    decayed = wait_until(lambda: app.engine.active_voices == 0, 30.0)
    after = (stream.frames - at_pedal_up) / stream.samplerate
    # Bounded in *rendered* audio too: these buffers still have ~4 s to run, so
    # "they went away eventually" would not prove the pedal-up released them.
    check("pedal release lets them decay", decayed and after < 2.0,
          f"{app.engine.active_voices} voices after {after:.2f}s of audio")

    app.timer.fire()
    check("note counter is shown in the menu", "4 notes" in app.status_item.title,
          app.status_item.title)

    print("\n[3] recording")
    check("export is disabled before recording", app.save_midi_item.callback is None)
    app.record_item.click()
    check("record toggle flips the label", app.record_item.title == "Stop Recording")
    app.timer.fire()
    check("menu bar title shows the record indicator", app.title.startswith(ICON_REC),
          app.title)

    for i, note in enumerate((62, 65, 69, 74, 72, 69, 65, 62)):
        _FakeMidiIn.send(NOTE_ON, note, 70 + i * 5)
        time.sleep(0.08)
        _FakeMidiIn.send(NOTE_OFF, note, 0)
    app.record_item.click()
    check("record toggle resets", app.record_item.title == "Start Recording")
    app.timer.fire()
    check("take summary appears in the menu", "8 notes" in app.take_item.title,
          app.take_item.title)
    check("export is enabled after recording", app.save_midi_item.callback is not None)

    print("\n[4] exporting")
    app.save_midi_item.click()
    mid_path = saved_paths[-1]
    check("midi file written", os.path.exists(mid_path) and os.path.getsize(mid_path) > 80,
          os.path.basename(mid_path))

    import mido
    reloaded = mido.MidiFile(mid_path)
    on_msgs = [m for tr in reloaded.tracks for m in tr
               if m.type == "note_on" and m.velocity > 0]
    check("all 8 played notes are in the file", len(on_msgs) == 8, f"{len(on_msgs)}")
    check("velocities survived", [m.velocity for m in on_msgs] ==
          [70 + i * 5 for i in range(8)])

    # A cancelled save panel returns None and must be a silent no-op.
    rumps.notifications.clear()
    macui.save_panel = lambda *a, **kw: None
    exports_before = len(saved_paths)
    raised = None
    try:
        app.save_midi_item.click()
    except Exception as exc:                      # a crash is the bug, not a pass
        raised = exc
    macui.save_panel = fake_save_panel
    check("cancelling the save panel exports nothing and does not raise",
          raised is None and len(saved_paths) == exports_before
          and not rumps.notifications, f"{raised!r} / {rumps.notifications}")

    take_seconds = rec.Recorder.duration(app.recorder.snapshot("take"))
    rumps.notifications.clear()
    app.save_wav_item.click()
    wav_path = saved_paths[-1]
    # `_busy` is cleared in the worker's `finally`, so it says nothing about the
    # outcome: assert the notification and the file itself.
    check("wav render finished", wait_until(lambda: not app._busy, 120.0, 0.1),
          f"busy={app._busy!r}")
    app.timer.fire()          # the tick posts the worker's result
    check("wav export reported success rather than an error",
          any("Audio exported" in str(n) for n in rumps.notifications)
          and not any("Export failed" in str(n) for n in rumps.notifications),
          str(rumps.notifications))
    frames = sr = channels = 0
    data = np.zeros(0)
    wav_error = None
    try:
        with wave.open(wav_path) as wav:
            frames, sr, channels = wav.getnframes(), wav.getframerate(), wav.getnchannels()
            data = np.frombuffer(wav.readframes(frames), dtype="<i2") / 32768.0
    except Exception as exc:
        wav_error = exc
    check("wav file is a valid non-empty 44.1 kHz stereo file",
          wav_error is None and frames > 0 and sr == 44100 and channels == 2,
          f"{wav_error!r} frames={frames} sr={sr} ch={channels}")
    check("wav covers the whole take plus the 3 s tail",
          sr and abs(frames / sr - (take_seconds + 3.0)) < 0.5,
          f"{frames / sr if sr else 0:.2f}s vs {take_seconds + 3.0:.2f}s")
    check("wav has audio", rms(data) > 0.005, f"rms={rms(data):.4f}")

    app.save_session_item.click()
    session_path = saved_paths[-1]
    session_mid = mido.MidiFile(session_path)
    session_ons = [m for tr in session_mid.tracks for m in tr
                   if m.type == "note_on" and m.velocity > 0]
    check("session export contains everything played (4 + 8)",
          len(session_ons) == 12, f"{len(session_ons)}")

    print("\n[5] overwrite confirmation")
    kept = app.recorder.snapshot("take")
    _Window.answer = 0                       # Cancel
    raised = None
    try:
        app.record_item.click()
    except Exception as exc:
        raised = exc
    check("cancelling the overwrite dialog keeps the un-exported take",
          raised is None and not app.recorder.recording
          and app.recorder.snapshot("take") == kept,
          f"{raised!r} recording={app.recorder.recording} "
          f"{len(app.recorder.snapshot('take'))}/{len(kept)} events")
    _Window.answer = 1                       # Replace
    app.record_item.click()
    check("confirming the overwrite starts a fresh take",
          app.recorder.recording and app.recorder.snapshot("take") == [],
          f"recording={app.recorder.recording} "
          f"{len(app.recorder.snapshot('take'))} events")
    for note in (55, 57):
        _FakeMidiIn.send(NOTE_ON, note, 84)
        time.sleep(0.05)
        _FakeMidiIn.send(NOTE_OFF, note, 0)
    app.record_item.click()
    app.timer.fire()
    check("the replacement take holds only the new notes",
          "2 notes" in app.take_item.title, app.take_item.title)

    if app.discard_item.callback is not None:   # greyed out without a take
        app.discard_item.click()
    app.timer.fire()
    check("discard clears the take", app.take_item.title == "No recording yet",
          app.take_item.title)
    check("export disabled again", app.save_midi_item.callback is None)

    print("\n[6] settings")
    app.sound_menu["Volume"]["50%"].click()
    check("volume applied to the engine", abs(app.engine.volume - 0.5) < 1e-9)
    check("volume checkmark moved", app.sound_menu["Volume"]["50%"].state == 1
          and app.sound_menu["Volume"]["75%"].state == 0)
    app.sound_menu["Reverb"]["Concert Hall"].click()
    check("reverb preset applied", app.engine.reverb.name == "hall")
    app.sound_menu["Touch Response"]["Heavy"].click()
    check("velocity curve applied", app.engine.velocity_curve == "hard")
    app.sound_menu["Latency"]["Lowest (~3 ms)"].click()
    check("blocksize applied and stream restarted", app.engine.blocksize == 128
          and len(_STREAMS) > 1 and _STREAMS[-1].blocksize == 128)

    _SD.attempts.clear()
    app.sound_menu["Audio Output"]["Fake Interface"].click()
    check("the chosen output device is the one that gets opened",
          app._audio_started and _SD.attempts == [1] and _STREAMS[-1].device == 1,
          f"attempts={_SD.attempts}")

    print("\n[7] audio device failure and fallback")
    # PortAudio refuses a device that vanished between query_devices() and
    # OutputStream(): _start_audio() must retry on the system default.
    _SD.fail_devices = {1}
    _SD.attempts.clear()
    raised = None
    try:
        app.sound_menu["Audio Output"]["Fake Interface"].click()
    except Exception as exc:
        raised = exc
    check("a failing device is caught and retried on the default",
          raised is None and _SD.attempts == [1, None], f"{raised!r} {_SD.attempts}")
    check("the fallback stream is running on the default device",
          app._audio_started and _STREAMS[-1].device is None,
          f"started={app._audio_started} device={_STREAMS[-1].device}")

    _SD.fail_devices = {0, 1, None}
    _SD.attempts.clear()
    raised = None
    try:
        app.sound_menu["Audio Output"]["System Default"].click()
    except Exception as exc:
        raised = exc
    check("a dead audio system leaves the app running without audio",
          raised is None and not app._audio_started and app.engine.stream is None,
          f"{raised!r} started={app._audio_started}")
    check("the PortAudio error is kept for the UI",
          bool(app.engine.error) and "Invalid device" in app.engine.error,
          str(app.engine.error)[:60])
    app.timer.fire()
    check("status line warns that there is no audio output",
          app.status_item.title.startswith("⚠️") and "No audio" in app.status_item.title,
          app.status_item.title)

    _SD.fail_devices = set()
    app.sound_menu["Audio Output"]["System Default"].click()
    check("audio recovers once the device comes back",
          app._audio_started and app.engine.stream is not None
          and _STREAMS[-1].device is None)
    app.timer.fire()
    check("the status line goes back to the keyboard",
          app.midi.port_name in app.status_item.title, app.status_item.title)

    # The latency the user is shown must come from the *stream* PortAudio handed
    # back, not from the blocksize (which would look plausible) and not from a
    # constant. `_STREAM_LATENCY_OVERHEAD` puts the stub's own latency well clear
    # of the blocksize fallback, so only a reading that really consults the
    # stream lands on this number -- in the menu bar and in the About box alike.
    live = _STREAMS[-1]
    expected_ms = (live.blocksize / live.samplerate + _STREAM_LATENCY_OVERHEAD) * 1000
    check("the reported latency is the stream's own, not the blocksize or a constant",
          abs(app.engine.latency_ms - expected_ms) < 0.05
          and f"{expected_ms:.0f} ms" in app.status_item.title,
          f"engine={app.engine.latency_ms:.2f} ms expected={expected_ms:.2f} ms "
          f"title={app.status_item.title!r}")
    app.menu["About Just Piano"].click()
    check("...and the About box quotes that same latency",
          any(f"~{expected_ms:.0f} ms" in text for _, text in rumps.alerts[-1:]),
          repr(rumps.alerts[-1] if rumps.alerts else None))

    persisted = {"volume": 0.5, "reverb": "hall", "velocity_curve": "hard",
                 "blocksize": 128}
    check("the persisted values differ from the defaults (so this can fail)",
          all(DEFAULTS[k] != v for k, v in persisted.items()), str(persisted))
    check("this run wrote its own settings file", os.path.exists(SETTINGS_PATH),
          SETTINGS_PATH)
    reread = Settings()
    check("settings persist to disk",
          all(reread[k] == v for k, v in persisted.items()),
          "/".join(f"{k}={reread[k]}" for k in persisted))

    print("\n[8] device hot-plug")
    stream = _STREAMS[-1]
    _FakeMidiIn.ports = ["IAC Driver Bus 1"]        # keyboard unplugged
    check("unplug is detected and the loopback is not grabbed",
          wait_until(lambda: app.midi.port_name is None, 15.0, 0.1)
          and app.midi.port_name is None, str(app.midi.port_name))
    # No manual `app._ports = ...`: the watcher's on_ports_changed callback is
    # what refreshes the app's copy, and that plumbing is under test too.
    check("the watcher pushed the new port list to the app",
          wait_until(lambda: app._ports == ["IAC Driver Bus 1"], 15.0, 0.1),
          str(app._ports))
    app.timer.fire()
    check("status line prompts the user", "Not connected" in app.status_item.title
          or "Plug in" in app.status_item.title, app.status_item.title)

    _FakeMidiIn.ports = ["Fake Studio Keys 88", "IAC Driver Bus 1"]  # plugged back in
    check("replug reconnects automatically",
          wait_until(lambda: app.midi.port_name == "Fake Studio Keys 88", 15.0, 0.1),
          str(app.midi.port_name))

    print("\n[9] a remembered keyboard is never silently swapped")
    _FakeMidiIn.ports = ["Fake Studio Keys 88", "Other Keys 61", "IAC Driver Bus 1"]
    wait_until(lambda: "Other Keys 61" in app._ports, 15.0, 0.1)
    app.timer.fire()
    app.midi_menu["Fake Studio Keys 88"].click()     # remember this one
    check("the remembered device is opened and stored",
          app.midi.port_name == "Fake Studio Keys 88"
          and app.midi.preferred == "Fake Studio Keys 88"
          and app.settings["midi_port"] == "Fake Studio Keys 88",
          f"{app.midi.port_name} / {app.settings['midi_port']}")

    _FakeMidiIn.ports = ["Other Keys 61", "IAC Driver Bus 1"]   # preferred unplugged
    check("unplugging the remembered keyboard does not latch onto another device",
          wait_until(lambda: app.midi.port_name != "Fake Studio Keys 88", 15.0, 0.1)
          and app.midi.port_name is None, str(app.midi.port_name))
    _FakeMidiIn.ports = ["Fake Studio Keys 88", "Other Keys 61", "IAC Driver Bus 1"]
    check("the watcher migrates back as soon as it reappears",
          wait_until(lambda: app.midi.port_name == "Fake Studio Keys 88", 15.0, 0.1),
          str(app.midi.port_name))
    app.midi_menu["Connect Automatically"].click()
    check("back to automatic connection",
          app.settings["midi_port"] is None and app.midi.preferred is None
          and app.midi.port_name == "Fake Studio Keys 88", str(app.midi.port_name))

    print("\n[10] two identical keyboards")
    check("unique_labels keeps every device selectable",
          unique_labels(["Twin Keys", "Twin Keys (2)", "Twin Keys"])
          == ["Twin Keys", "Twin Keys (2)", "Twin Keys (3)"],
          str(unique_labels(["Twin Keys", "Twin Keys (2)", "Twin Keys"])))
    _FakeMidiIn.ports = ["Twin Keys", "Twin Keys", "IAC Driver Bus 1"]
    check("the twins are picked up by the watcher",
          wait_until(lambda: app.midi.port_name == "Twin Keys", 15.0, 0.1),
          str(app.midi.port_name))
    # No hand-written _rebuild_midi_menu(): _tick's signature diff is the only
    # thing that rebuilds the menu in the real app, so let it do the work.
    app.timer.fire()
    labels = [t for t in app.midi_menu if not t.startswith("SeparatorMenuItem")]
    check("both identical ports are listed",
          "Twin Keys" in labels and "Twin Keys (2)" in labels, str(labels[:3]))
    second = app.midi_menu.get("Twin Keys (2)")
    if second is not None:
        second.click()
    check("the second twin is opened by index, not by name",
          second is not None and _FakeMidiIn.last_index == 1,
          f"opened index {_FakeMidiIn.last_index}")
    check("selecting a twin still connects", app.midi.port_name == "Twin Keys")

    print("\n[11] panic")
    delivered = all(_FakeMidiIn.send(NOTE_ON, note, 110) for note in range(48, 72))
    check("the panic chord is actually sounding",
          delivered and app.engine.active_voices > 0,
          f"delivered={delivered} {app.engine.active_voices} voices")
    stream = _STREAMS[-1]
    # Let the reverb tank fill first: with no rendered audio between the
    # note-ons and the click the comb buffers are still all-zero, so the tail
    # assertion below passed even when _panic() never reset the reverb.
    wait_rendered(stream, 0.30)
    check("the reverb tank has energy for panic to drop",
          any(float(np.abs(c).max()) > 1e-3 for c in stream.captured[-5:]),
          f"blocks={len(stream.captured)}")
    app.menu["Panic (All Notes Off)"].click()
    stream.captured.clear()
    check("panic stops every voice", app.engine.active_voices == 0,
          f"{app.engine.active_voices}")
    wait_until(lambda: len(stream.captured) >= 20, 10.0)
    tail = np.concatenate(stream.captured[-10:]) if stream.captured else np.ones((1, 2))
    check("panic also drops the reverb tail", float(np.abs(tail).max()) == 0.0,
          f"peak={float(np.abs(tail).max()):.2e}")

    print("\n[12] open at login")
    check("the login item is disabled until the app is bundled",
          app.login_item.callback is None
          and "build the .app first" in app.login_item.title, app.login_item.title)
    login = {"listed": False, "calls": [], "ok": True}

    def fake_set_login_item(path, enabled):
        login["calls"].append(enabled)
        if not login["ok"]:
            return False
        login["listed"] = enabled
        return True

    macui.bundle_path = lambda: "/Applications/JustPiano.app"
    macui.is_login_item = lambda path: login["listed"]
    macui.set_login_item = fake_set_login_item
    app.login_item.set_callback(app._toggle_login)   # _build_menu wires this on a Mac
    rumps.alerts.clear()
    app.login_item.click()
    check("enabling registers the bundle as a login item",
          login["calls"] == [True] and login["listed"]
          and app.login_item.state == 1 and app.settings["login_item"] is True,
          f"calls={login['calls']} state={app.login_item.state}")

    # The entry was removed in System Settings behind our back, so the persisted
    # flag is stale. Asking System Events to delete an item that is not there is
    # an error, which used to surface as a bogus permissions alert.
    login["listed"] = False
    login["calls"].clear()
    rumps.alerts.clear()
    app.login_item.click()
    check("a stale flag is reconciled without a bogus permissions alert",
          login["calls"] == [] and app.login_item.state == 0
          and app.settings["login_item"] is False and not rumps.alerts,
          f"calls={login['calls']} alerts={rumps.alerts}")

    # The query itself failed (Automation denied) -> is_login_item is None.
    # "Unknown" must not be read as "already in the state you asked for": that
    # skipped the write, so the checkmark went off while macOS kept launching
    # the app at login.
    macui.is_login_item = lambda path: None
    app.login_item.click()
    check("an unknown login-item state still writes the change",
          login["calls"] == [True] and login["listed"] is True
          and app.login_item.state == 1 and app.settings["login_item"] is True
          and not rumps.alerts,
          f"calls={login['calls']} state={app.login_item.state} "
          f"alerts={rumps.alerts}")
    macui.is_login_item = lambda path: login["listed"]
    app.login_item.click()
    check("and it can be switched back off once the answer is known again",
          login["calls"] == [True, False] and login["listed"] is False
          and app.login_item.state == 0
          and app.settings["login_item"] is False,
          f"calls={login['calls']} state={app.login_item.state}")
    login["calls"].clear()
    rumps.alerts.clear()

    login["ok"] = False
    app.login_item.click()
    check("a refused change is reported and not remembered",
          login["calls"] == [True] and app.login_item.state == 0
          and app.settings["login_item"] is False
          and any("login items" in str(a) for a in rumps.alerts),
          f"state={app.login_item.state} alerts={rumps.alerts}")
    macui.bundle_path = lambda: None

    print("\n[13] MIDI port failures")
    import justpiano.midi_in as midi_in_mod
    from justpiano.midi_in import MidiInput

    # A standalone input driven by explicit calls only: the app's own 1.5 s poll
    # would otherwise interleave opens into the event log below.
    app.midi.stop_watcher()
    check("the app's watcher can be stopped for the duration",
          wait_until(lambda: not app.midi._watcher.is_alive(), 10.0, 0.05))
    _FakeMidiIn.ports = ["Studio Keys 88", "Spare Keys 61", "IAC Driver Bus 1"]
    _FakeMidiIn.fail_ports = set()
    _FakeMidiIn.events.clear()
    probe = MidiInput(lambda *a: None)
    check("auto-pick opens real hardware and skips the loopback",
          probe.open(None) is True and probe.port_name == "Studio Keys 88",
          str(probe.port_name))
    # An explicit request for a device that is gone must fail: substituting
    # another keyboard here contradicts the watcher's preferred-only policy and
    # shows up as a connect-then-drop flap one tick later.
    check("a requested device that is absent does not fall back to the auto-pick",
          probe.open("Ghost Keys 76") is False
          and probe.open(None, index=9) is False
          and probe.port_name == "Studio Keys 88" and probe.error is None,
          f"{probe.port_name} / {probe.error!r}")

    _FakeMidiIn.events.clear()
    switched = probe.open("Spare Keys 61", index=1)
    check("switching keyboards opens the replacement before releasing the old one",
          switched is True and probe.port_name == "Spare Keys 61"
          and _FakeMidiIn.events == [("open", "Spare Keys 61"),
                                     ("close", "Studio Keys 88")],
          str(_FakeMidiIn.events))

    _FakeMidiIn.fail_ports = {"Studio Keys 88"}
    refused = probe.open("Studio Keys 88", index=0)
    check("a refused port never costs the caller its working connection",
          refused is False and probe.port_name == "Spare Keys 61"
          and any(inst.port == "Spare Keys 61" for inst in _FakeMidiIn.live),
          f"{probe.port_name} / refused={refused}")
    check("the refusal names the port and quotes the reason",
          probe.error is not None
          and probe.error.startswith('Could not open "Studio Keys 88": ')
          and "openPort" in probe.error, repr(probe.error))
    # A refusal is a *timestamped* note, not a bare name: it is what lets the
    # entry go stale (see below), so the check compares the key set and the shape
    # of the value rather than the dict itself.
    refused_at = probe._open_failed.get("Studio Keys 88")
    check("a refused port is remembered so the auto-pick moves on",
          set(probe._open_failed) == {"Studio Keys 88"}
          and isinstance(refused_at, float)
          and 0.0 <= time.monotonic() - refused_at < 5.0
          and MidiInput._auto_pick(probe.list_ports(), probe._open_failed)
          == "Spare Keys 61", str(probe._open_failed))
    check("a refusal that has not aged out yet is still in force",
          set(probe._refusals()) == {"Studio Keys 88"}
          and MidiInput._auto_pick(probe.list_ports(), probe._refusals())
          == "Spare Keys 61", str(probe._open_failed))
    # A refusal that never expires latches the keyboard out for the whole
    # session: the app holding it quits, the port list never changes, and the
    # auto-pick keeps stepping over the only device the user owns.
    probe._open_failed["Studio Keys 88"] = (time.monotonic()
                                           - midi_in_mod._REFUSAL_TTL - 0.5)
    check("a refusal expires after the TTL so a released keyboard is retried",
          probe._refusals() == {} and probe._open_failed == {}
          and MidiInput._auto_pick(probe.list_ports(), probe._refusals())
          == "Studio Keys 88", str(probe._open_failed))
    # An explicit auto request -- launch, "Connect Automatically", "Rescan
    # Devices" -- is the user asking us to try everything again, so it forgives
    # every refusal instead of quietly skipping the port. Still busy, so it lands
    # back in the dict, and without costing the caller the live keyboard.
    probe._open_failed["Studio Keys 88"] = time.monotonic()
    probe.error = None
    rescanned = probe.open(None)
    check("an explicit rescan clears the refusals and re-tries the port",
          rescanned is False
          and probe.error is not None and "Studio Keys 88" in probe.error
          and set(probe._open_failed) == {"Studio Keys 88"}
          and probe.port_name == "Spare Keys 61",
          f"{rescanned} / {probe.error!r} / {probe.port_name}")
    # The reason is only useful if the menu actually shows it: a port claimed
    # by another app otherwise looks identical to "nothing plugged in".
    app.midi.close()
    app.midi.error = 'Could not open "Studio Keys 88": busy'
    app._tick(None)
    check("the status line explains why a keyboard would not open",
          "Studio Keys 88" in app.status_item.title
          and "busy" in app.status_item.title, app.status_item.title)
    app.midi.error = None
    # ...and forgiven as soon as the device list changes: a keyboard that was
    # busy once must not be skipped for the rest of the session.
    _FakeMidiIn.fail_ports = set()
    probe.start_watcher(0.05)
    _FakeMidiIn.ports = ["Studio Keys 88", "Spare Keys 61", "Spare Keys 49",
                         "IAC Driver Bus 1"]
    check("a changed port list gives a refused device another chance",
          wait_until(lambda: probe.port_name == "Studio Keys 88", 15.0, 0.05),
          f"{probe.port_name} failed={probe._open_failed}")
    probe.shutdown()
    check("a fresh input is terminal after shutdown too",
          probe.open(None) is False and probe.port_name is None
          and probe.open_virtual("Just Piano Probe") is False)

    _FakeMidiIn.ports = ["Twin Keys", "Twin Keys", "IAC Driver Bus 1"]
    app.midi.close()
    app.midi.start_watcher()
    check("the app's watcher reconnects after being restarted",
          wait_until(lambda: app.midi.port_name == "Twin Keys", 15.0, 0.1),
          str(app.midi.port_name))

    print("\n[14] failures the user has to see")
    import justpiano.tray as tray_mod

    # A take holding nothing but pedal moves is still a take: it has to stay
    # exportable and discardable instead of being silently unreachable.
    app.record_item.click()
    _FakeMidiIn.send(CC, 64, 127)
    _FakeMidiIn.send(CC, 64, 0)
    check("the pedal-only events reached the recorder",
          wait_until(lambda: len(app.recorder.snapshot("take")) >= 2, 5.0, 0.02),
          f"{len(app.recorder.snapshot('take'))} events")
    app.record_item.click()
    app.timer.fire()
    check("a take with no notes says so instead of 'No recording yet'",
          "no notes" in app.take_item.title, app.take_item.title)
    check("a pedal-only take is still exportable and discardable",
          app.save_midi_item.callback is not None
          and app.save_wav_item.callback is not None
          and app.discard_item.callback is not None,
          f"midi={app.save_midi_item.callback is not None} "
          f"wav={app.save_wav_item.callback is not None} "
          f"discard={app.discard_item.callback is not None}")

    # A WAV render that fails has to be modal: a Notification Center banner can
    # be suppressed system-wide, and then the export just never happened.
    real_export_wav = rec.export_wav

    def exploding_export_wav(*_a, **_kw):
        raise ValueError("piano samples are still being built")

    rec.export_wav = exploding_export_wav
    rumps.alerts.clear()
    rumps.notifications.clear()
    click_if_enabled(app.save_wav_item)
    check("the failed render releases the busy flag",
          wait_until(lambda: not app._busy, 30.0, 0.05), f"busy={app._busy!r}")
    app.timer.fire()
    check("a failed WAV render is alerted, not left to a droppable banner",
          any("Export failed" in str(a) for a in rumps.alerts)
          and not any("Audio exported" in str(n) for n in rumps.notifications),
          f"alerts={rumps.alerts} notifications={rumps.notifications}")
    rec.export_wav = real_export_wav

    # The renderer thread refuses to start (thread limit reached). Its `finally`
    # never runs, so _export has to clear `_busy` itself: a wedged flag blocks
    # every later export *and* Quit, and Quit is the only way out of an app
    # built with quit_button=None.
    class _DeadThread:
        def __init__(self, *_a, **_kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    real_threading = tray_mod.threading
    tray_mod.threading = types.SimpleNamespace(Thread=_DeadThread,
                                               Lock=real_threading.Lock)
    rumps.alerts.clear()
    raised = None
    try:
        click_if_enabled(app.save_wav_item)
    except Exception as exc:
        raised = exc
    finally:
        tray_mod.threading = real_threading
    check("a render thread that cannot start is reported instead of raising",
          raised is None and any("Export failed" in str(a) for a in rumps.alerts),
          f"{raised!r} alerts={rumps.alerts}")
    rumps.alerts.clear()
    exports_before = len(saved_paths)
    click_if_enabled(app.save_midi_item)
    check("and the app is not wedged into 'Still rendering audio…' afterwards",
          not app._busy and len(saved_paths) == exports_before + 1
          and not any("Please wait" in str(a) for a in rumps.alerts),
          f"busy={app._busy!r} alerts={rumps.alerts}")
    click_if_enabled(app.discard_item)
    app.timer.fire()
    check("the pedal-only take can be discarded like any other",
          app.take_item.title == "No recording yet"
          and app.save_wav_item.callback is None, app.take_item.title)

    # Settings.save() must not raise from a menu callback, so the status line is
    # the only place a read-only support folder can ever surface.
    from justpiano import config as cfg
    good_settings_path = cfg.SETTINGS_PATH
    cfg.SETTINGS_PATH = os.path.join(good_settings_path, "no-such-dir",
                                     "settings.json")
    app.sound_menu["Volume"]["25%"].click()
    check("a failed settings write leaves a reason behind",
          bool(app.settings.error), repr(app.settings.error))
    app.timer.fire()
    # Corrected expectation: `Settings.error` carries read and validation
    # problems too ("holds no Just Piano settings", "volume 400% is out of
    # range"), so the status line says "Settings:" and quotes the reason
    # verbatim. Asserting "Settings not saved" would demand a sentence that is
    # wrong for two of the three sources of that string.
    check("the status line surfaces settings that are not being saved",
          app.status_item.title.startswith("⚠️ Settings: ")
          and app.settings.error in app.status_item.title,
          app.status_item.title)
    cfg.SETTINGS_PATH = good_settings_path
    app.sound_menu["Volume"]["50%"].click()
    app.timer.fire()
    check("the warning clears once a write succeeds",
          app.settings.error is None
          and "Settings" not in app.status_item.title,
          app.status_item.title)
    # The reason the wording had to change: a value that came back out of range
    # is neither a save failure nor silently acceptable -- the file was written
    # fine, the stored number was not.
    app.settings["volume"] = 9.5
    app.timer.fire()
    check("a settings value that had to be corrected reaches the same status line",
          app.settings.error is not None
          and "volume" in app.settings.error.lower()
          and app.status_item.title.startswith("⚠️ Settings: ")
          and os.path.exists(cfg.SETTINGS_PATH),
          f"{app.settings.error!r} / {app.status_item.title}")
    app.sound_menu["Volume"]["50%"].click()
    app.timer.fire()
    check("and it clears again on the next good value",
          app.settings.error is None and "Settings" not in app.status_item.title,
          app.status_item.title)

    print("\n[15] instrument")
    from justpiano import tone

    voicing_menu = app.sound_menu[VOICING_MENU_TITLE]
    labels = [tone.VOICING_LABELS[v] for v in tone.VOICINGS]
    listed = [t for t in voicing_menu if not t.startswith("SeparatorMenuItem")]
    check("every voicing has its own menu entry, labelled from the tone model",
          listed == labels and all(voicing_menu[t].callback is not None
                                   for t in listed),
          str(listed))
    check("the voicing the app booted on is the only one checked",
          app.settings["voicing"] == tone.DEFAULT_VOICING
          and app.bank.voicing == tone.DEFAULT_VOICING
          and all(voicing_menu[tone.VOICING_LABELS[v]].state
                  == (1 if v == tone.DEFAULT_VOICING else 0) for v in tone.VOICINGS),
          f"{app.bank.voicing} / "
          f"{[voicing_menu[t].state for t in listed]}")

    # The export state machine owns the bank while a WAV renders (the worker
    # reads self.bank on its own thread), so a swap has to be refused there --
    # the same answer a second export gets.
    app._busy = "rendering audio"
    rumps.alerts.clear()
    bank_before = app.bank
    voicing_menu[tone.VOICING_LABELS["felt"]].click()
    check("a voicing switch is refused while an export is in flight",
          app.bank is bank_before and app.settings["voicing"] == tone.DEFAULT_VOICING
          and voicing_menu[tone.VOICING_LABELS["felt"]].state == 0
          and any("Please wait" in str(a) for a in rumps.alerts),
          f"voicing={app.settings['voicing']} alerts={rumps.alerts}")
    app._busy = ""

    # Hold a pedalled bass chord across the swap: a voice that survived would
    # keep mixing numpy views into the bank that is being replaced, and the note
    # would be stuck on top of the new voicing.
    stream = _STREAMS[-1]
    _FakeMidiIn.send(CC, 64, 127)
    for note in (28, 33, 40):
        _FakeMidiIn.send(NOTE_ON, note, 100)
    wait_rendered(stream, 0.2)
    check("a pedalled chord is sounding when the voicing is switched",
          app.engine.active_voices == 3 and app.engine.sustain,
          f"{app.engine.active_voices} voices, sustain={app.engine.sustain}")

    swap_state = []
    real_bank_cls = tray_mod.SampleBank

    class _WatchedBank(real_bank_cls):
        """Records what the engine looked like at the instant of the swap."""

        def __init__(self, *a, **kw):
            swap_state.append((app.engine.stream is None,
                               app.engine.active_voices, app.engine.sustain))
            super().__init__(*a, **kw)

    gate = threading.Event()
    real_render = tone.render_note

    def gated_render(*a, **kw):
        gate.wait(60.0)                    # a first-time voicing, held open
        return real_render(*a, **kw)

    tray_mod.SampleBank = _WatchedBank
    tone.render_note = gated_render
    try:
        t0 = time.monotonic()
        voicing_menu[tone.VOICING_LABELS["felt"]].click()
        click_seconds = time.monotonic() - t0
        check("picking a first-time voicing returns at once instead of blocking "
              "the menu on the render",
              click_seconds < 2.0 and not app.bank.ready
              and app.bank.voicing == "felt",
              f"{click_seconds * 1000:.0f} ms, ready={app.bank.ready}")
        check("the bank is swapped with the callback stopped and every voice dropped",
              swap_state == [(True, 0, False)], str(swap_state))
        check("the choice is remembered and the checkmark moves with it",
              app.settings["voicing"] == "felt"
              and voicing_menu[tone.VOICING_LABELS["felt"]].state == 1
              and voicing_menu[tone.VOICING_LABELS[tone.DEFAULT_VOICING]].state == 0,
              f"{app.settings['voicing']} / "
              f"{[voicing_menu[t].state for t in listed]}")
        check("the engine plays from the new bank and the stream is running again",
              app.engine.bank is app.bank and app._audio_started
              and app.engine.stream is not None and _STREAMS[-1] is not stream,
              f"started={app._audio_started}")
        swapped = _STREAMS[-1]
        swapped.captured.clear()
        wait_rendered(swapped, 0.2)
        after = (np.concatenate(swapped.captured) if swapped.captured
                 else np.ones((1, 2)))
        check("nothing is left ringing, torn or NaN after the swap",
              np.isfinite(after).all() and float(np.abs(after).max()) == 0.0
              and app.engine.active_voices == 0,
              f"peak={float(np.abs(after).max()):.2e}, "
              f"{app.engine.active_voices} voices")
        app.timer.fire()
        check("a first-time voicing surfaces the existing build progress",
              "Building piano" in app.status_item.title
              and app.title.startswith(ICON_IDLE) and "%" in app.title,
              f"{app.status_item.title!r} {app.title!r}")
    finally:
        gate.set()
        tone.render_note = real_render
        tray_mod.SampleBank = real_bank_cls

    check("the first-time voicing finishes building in the background",
          wait_until(lambda: app.bank.ready, BANK_BUILD_TIMEOUT, 0.1)
          and app.bank.error is None,
          f"progress={app.bank.progress:.0%} error={app.bank.error!r}")
    stream = _STREAMS[-1]
    stream.captured.clear()
    for note in (55, 60, 64):
        _FakeMidiIn.send(NOTE_ON, note, 100)
    wait_rendered(stream, 0.3)
    played = np.concatenate(stream.captured) if stream.captured else np.zeros((1, 2))
    check("the keyboard is playable on the voicing that was just built",
          app.engine.active_voices == 3 and rms(played) > 1e-3
          and np.isfinite(played).all(),
          f"{app.engine.active_voices} voices, rms={rms(played):.4f}")
    app.menu["Panic (All Notes Off)"].click()

    # A voicing whose render fails must say so instead of freezing on a
    # percentage, and must leave the app running and recoverable.
    def explode(*_a, **_kw):
        raise MemoryError("cannot allocate 2.1 GiB")

    quiet, threading.excepthook = threading.excepthook, lambda args: None
    tone.render_note = explode
    try:
        voicing_menu[tone.VOICING_LABELS["upright"]].click()
        check("a voicing whose build fails records the error",
              wait_until(lambda: app.bank.error is not None, 30.0, 0.05)
              and not app.bank.ready and app.bank.voicing == "upright",
              f"error={app.bank.error!r} ready={app.bank.ready}")
        app.timer.fire()
        check("a failed voicing build surfaces the samples-failed status line",
              app.status_item.title.startswith("⚠️")
              and "Piano samples failed" in app.status_item.title
              and "allocate" in app.status_item.title, app.status_item.title)
        check("the app keeps its audio output through a failed voicing build",
              app._audio_started and app.engine.stream is not None)
    finally:
        tone.render_note = real_render
        threading.excepthook = quiet

    # Switching back to a voicing that has been built before is a cache read:
    # not one note may be re-rendered.
    renders = []

    def counting_render(*a, **kw):
        renders.append(a[:2])
        return real_render(*a, **kw)

    tone.render_note = counting_render
    try:
        voicing_menu[tone.VOICING_LABELS[tone.DEFAULT_VOICING]].click()
        reloaded_ok = wait_until(lambda: app.bank.ready, 30.0, 0.05)
        voicing_menu[tone.VOICING_LABELS["felt"]].click()
        reloaded_ok = wait_until(lambda: app.bank.ready, 30.0, 0.05) and reloaded_ok
    finally:
        tone.render_note = real_render
    check("a voicing built earlier is reloaded from its own cache, not rebuilt",
          reloaded_ok and not renders and app.bank.voicing == "felt"
          and app.bank.error is None,
          f"{len(renders)} notes re-rendered, voicing={app.bank.voicing}")
    check("a non-default voicing survives a re-read of settings.json",
          Settings()["voicing"] == "felt" and DEFAULTS["voicing"] != "felt",
          f'{Settings()["voicing"]!r} vs default {DEFAULTS["voicing"]!r}')

    # Re-picking the voicing that is already playing must not cost a rebuild
    # (1-2 s of render) or a stream restart.
    bank_before, streams_before = app.bank, len(_STREAMS)
    voicing_menu[tone.VOICING_LABELS["felt"]].click()
    check("re-picking the active voicing is a no-op",
          app.bank is bank_before and len(_STREAMS) == streams_before
          and voicing_menu[tone.VOICING_LABELS["felt"]].state == 1,
          f"streams={len(_STREAMS)}/{streams_before}")

    # A fresh launch has to come up on the remembered voicing (out of its own
    # cache), not on the default one.
    relaunched = JustPianoApp()
    try:
        check("a fresh launch boots on the remembered voicing",
              relaunched.bank.voicing == "felt"
              and relaunched.sound_menu[VOICING_MENU_TITLE][
                  tone.VOICING_LABELS["felt"]].state == 1
              and wait_until(lambda: relaunched.bank.ready, BANK_BUILD_TIMEOUT, 0.1),
              f"{relaunched.bank.voicing}, ready={relaunched.bank.ready}")
    finally:
        relaunched._quit(None)

    print("\n[16] on-screen keyboard panel")
    from justpiano import keyboardview

    # Graceful degradation first: there is no pyobjc on this machine, which is
    # exactly the situation keyboardview promises to survive.
    check("keyboardview imports without pyobjc and says AppKit is unavailable",
          keyboardview.available() is False)
    check("without AppKit no panel is built and nothing is re-wired",
          keyboardview.create_panel(app.keys, object()) is None
          and keyboardview.intercept_status_item(object(),
                                                 lambda secondary: None) is None)

    panels: list = []
    handlers: list = []

    def fake_create_panel(controller, status_item):
        # The AppKit half cannot run here; everything around it can.
        panels.append(_FakePanel(controller))
        return panels[-1]

    def fake_click_handler(on_click):
        handlers.append(_FakeClickHandler(on_click))
        return handlers[-1]

    real_create_panel = keyboardview.create_panel
    real_make_click_handler = keyboardview._make_click_handler
    keyboardview.create_panel = fake_create_panel
    keyboardview._make_click_handler = fake_click_handler

    app._rumps_run_setup()          # what rumps.App.run() does before the loop
    status_item = app._nsapp.nsstatusitem
    check("rumps hands the whole menu to the status item at launch",
          status_item.menu() is app.menu._menu and app.panel is None,
          f"menu={status_item.menu()!r} panel={app.panel!r}")
    before_start = rumps.events.before_start
    before_start.errors.clear()
    before_start.emit()
    check("the panel is installed from rumps' before_start event, without error",
          bool(panels) and app.panel is panels[-1] and not before_start.errors,
          f"errors={before_start.errors}")
    button = status_item.button()
    check("the click is taken off the menu and given to the status item button",
          status_item.menu() is None and button.target is handlers[-1]
          and button.action == "statusItemClicked:",
          f"menu={status_item.menu()!r} action={button.action!r}")
    check("the button is told to report right clicks as well as left ones",
          button.action_mask == keyboardview.CLICK_MASK
          and bool(button.action_mask & (1 << 2))
          and bool(button.action_mask & (1 << 4)), f"mask={button.action_mask}")

    panel, handler = panels[-1], handlers[-1]
    board = app.keys.keyboard
    check("the panel draws a full 88-key piano",
          len(board.keys) == 88 and len(board.white_keys) == 52
          and len(board.black_keys) == 36,
          f"{len(board.white_keys)} white / {len(board.black_keys)} black")

    handler.statusItemClicked_(button)
    check("a left click opens the keyboard instead of dropping the menu",
          panel.is_open and panel.opens == 1 and not status_item.popped
          and app.panel_timer.started,
          f"open={panel.is_open} popped={len(status_item.popped)}")
    check("the keyboard is refreshed fast enough to look played, the menu is not",
          app.panel_timer.interval <= 1 / 30.0 and app.timer.interval == 0.5,
          f"{app.panel_timer.interval:.4f}s vs {app.timer.interval}s")

    panel.redraws = 0
    delivered = _FakeMidiIn.send(NOTE_ON, 60, 100)
    app.panel_timer.fire()
    check("a note played on the MIDI keyboard lights its key on the next frame",
          delivered and app.keys.lit.get(60) == 100 and panel.redraws == 1,
          f"delivered={delivered} lit={sorted(app.keys.lit)} "
          f"redraws={panel.redraws}")
    app.panel_timer.fire()
    check("a frame in which nothing moved does not repaint", panel.redraws == 1,
          f"redraws={panel.redraws}")
    _FakeMidiIn.send(NOTE_OFF, 60, 0)
    app.panel_timer.fire()
    check("releasing the key unlights it",
          60 not in app.keys.lit and panel.redraws == 2,
          f"lit={sorted(app.keys.lit)} redraws={panel.redraws}")

    middle_c, d_above = board.rect(60), board.rect(62)
    app.record_item.click()             # a mouse note must record like any other
    notes_before = app._notes_played
    app.keys.mouse_down(middle_c.center_x, board.height - 1.0)
    loud = app.keys.lights.snapshot().get(60)
    check("clicking a key plays it, and playing it low on the key plays it hard",
          app.keys.mouse_note == 60 and loud is not None and loud > 110
          and app.engine.active_voices >= 1
          and app._notes_played == notes_before + 1,
          f"velocity={loud} voices={app.engine.active_voices}")
    app.keys.mouse_dragged(d_above.center_x, board.height - 1.0)
    lit = app.keys.lights.snapshot()
    check("dragging across the keys glissandos instead of stacking notes",
          app.keys.mouse_note == 62 and 60 not in lit and 62 in lit,
          f"down={sorted(lit)} mouse={app.keys.mouse_note}")
    app.keys.mouse_up()
    check("releasing the mouse stops the note it was playing",
          app.keys.mouse_note is None and 62 not in app.keys.lights.snapshot(),
          f"down={sorted(app.keys.lights.snapshot())}")
    app.keys.mouse_down(middle_c.center_x, 2.0)
    soft = app.keys.lights.snapshot().get(60)
    app.keys.mouse_up()
    check("clicking at the top of a key is much quieter than at the front edge",
          soft is not None and soft < 60 and loud - soft > 40,
          f"{soft} at the top vs {loud} at the front")
    app.record_item.click()
    app.timer.fire()
    check("notes played with the mouse are recorded like any others",
          "3 notes" in app.take_item.title, app.take_item.title)
    click_if_enabled(app.discard_item)

    status_item.popped.clear()
    app.keys.settings_clicked()
    check("the gear button opens the menu and puts the keyboard away",
          status_item.popped == [app.menu._menu] and not panel.is_open
          and not app.panel_timer.started,
          f"popped={len(status_item.popped)} open={panel.is_open}")
    required = ("MIDI Keyboard", "Sound", "Start Recording", "Export MIDI File…",
                "Export Audio (WAV)…", "Discard Recording",
                "Export Everything Since Launch…", "Open Recordings Folder",
                "Panic (All Notes Off)", "Open at Login", "About Just Piano",
                "Quit")
    sound = (VOICING_MENU_TITLE, "Volume", "Reverb", "Touch Response", "Latency",
             "Audio Output")
    check("the menu the gear opens is the original one, every item intact",
          all(t in app.menu for t in required)
          and all(t in app.sound_menu for t in sound),
          str([t for t in required + sound
               if t not in app.menu and t not in app.sound_menu]))
    check("Quit is still the app's own shutdown callback, not orphaned",
          app.menu["Quit"].callback is not None
          and app.menu["Quit"].callback.__func__ is JustPianoApp._quit,
          repr(app.menu["Quit"].callback))

    handler.secondary = True
    status_item.popped.clear()
    handler.statusItemClicked_(button)
    check("a right click on the icon still gets to the menu",
          status_item.popped == [app.menu._menu] and not panel.is_open,
          f"popped={len(status_item.popped)}")
    handler.secondary = False

    handler.statusItemClicked_(button)
    check("the panel opens again after the menu was used",
          panel.is_open and app.panel_timer.started)
    # A click *away* from the icon: AppKit dismisses the popover and no action is
    # on its way, so there is nothing for the tray to consume.
    panel.dismiss(on_icon=False)
    app.panel_timer.fire()
    check("a panel dismissed by clicking away stops the 60 Hz timer",
          not app.panel_timer.started and not panel.is_open
          and panel.dismissed_at is None,
          f"timer={app.panel_timer.started} stamp={panel.dismissed_at}")

    # ---- closing the keyboard by clicking the icon again.
    #
    # The order here is the bug: NSPopoverBehaviorTransient dismisses itself on
    # the mouse-*down*, and the status item only reports the click on mouse-*up*
    # (keyboardview.CLICK_MASK), so by the time `statusItemClicked:` arrives the
    # popover is already gone. Reading `panel.is_open` alone therefore said "not
    # open" and reopened the keyboard that the very same click had just closed -
    # a blink and a restarted 60 Hz timer. This check used to deliver the click
    # *without* dismissing first, which is a sequence no Mac ever produces, and
    # that is why it passed on the inverted behaviour too.
    handler.statusItemClicked_(button)
    check("precondition: the keyboard is on screen and being redrawn",
          panel.is_open and app.panel_timer.started)
    opens_before, forgotten_before = panel.opens, panel.forgotten
    panel.dismiss()                     # the mouse-down, on the icon
    handler.statusItemClicked_(button)  # ...and the mouse-up that follows it
    check("clicking the icon while the keyboard is up leaves it closed",
          not panel.is_open and panel.opens == opens_before
          and not app.panel_timer.started,
          f"open={panel.is_open} opens={panel.opens - opens_before} "
          f"timer={app.panel_timer.started}")
    check("...and the dismissal is consumed, so the next click opens it again",
          panel.dismissed_at is None and panel.forgotten == forgotten_before + 1,
          f"stamp={panel.dismissed_at} forgotten={panel.forgotten}")
    handler.statusItemClicked_(button)
    check("the click after that one does open the keyboard",
          panel.is_open and panel.opens == opens_before + 1
          and app.panel_timer.started, f"opens={panel.opens}")

    # A stamp older than the grace window is two separate clicks: away first
    # (which closed it), then a trip to the menu bar and a click on the icon.
    # That second click is a plain request to open the keyboard.
    panel.dismiss(when=time.monotonic() - tray_mod.PANEL_DISMISS_GRACE - 0.05)
    check("precondition: the stale stamp is there to be ignored",
          panel.dismissed_at is not None and not panel.is_open)
    handler.statusItemClicked_(button)
    check("a stale dismissal is not mistaken for this click",
          panel.is_open and app.panel_timer.started,
          f"open={panel.is_open} grace={tray_mod.PANEL_DISMISS_GRACE}s")
    check("the grace window is long enough for a click and short enough for two",
          0.1 <= tray_mod.PANEL_DISMISS_GRACE <= 0.5,
          f"{tray_mod.PANEL_DISMISS_GRACE}s")

    # A close *we* asked for (the gear, or the tray putting the keyboard away for
    # the menu) must not look like a user dismissal: it would swallow the click
    # that comes next and leave the icon apparently dead.
    status_item.popped.clear()
    app.keys.settings_clicked()         # gear -> _show_menu -> _close_panel
    check("a close the app itself asked for leaves no dismissal behind",
          not panel.is_open and panel.dismissed_at is None
          and status_item.popped == [app.menu._menu],
          f"stamp={panel.dismissed_at}")
    handler.statusItemClicked_(button)
    check("...so the click after the gear opens the keyboard, not nothing",
          panel.is_open and app.panel_timer.started)
    app._close_panel()
    check("_close_panel leaves no stamp either",
          panel.dismissed_at is None and not app.panel_timer.started)

    handler.statusItemClicked_(button)
    app.keys.mouse_down(middle_c.center_x, board.height - 1.0)
    handler.statusItemClicked_(button)
    check("clicking the icon again closes the panel and lets go of the key",
          not panel.is_open and not app.panel_timer.started
          and app.keys.mouse_note is None
          and 60 not in app.keys.lights.snapshot(),
          f"open={panel.is_open} mouse={app.keys.mouse_note}")

    handler.statusItemClicked_(button)
    for note in (52, 57, 64):
        _FakeMidiIn.send(NOTE_ON, note, 88)
    app.panel_timer.fire()
    check("a chord lights every key it sounds", len(app.keys.lit) == 3,
          f"lit={sorted(app.keys.lit)}")
    _FakeMidiIn.send(CC, 123, 0)
    app.panel_timer.fire()
    check("All Notes Off puts the keys back up", not app.keys.lit,
          f"lit={sorted(app.keys.lit)}")
    for note in (40, 44):
        _FakeMidiIn.send(NOTE_ON, note, 88)
    app.menu["Panic (All Notes Off)"].click()
    app.panel_timer.fire()
    check("panic puts every key back up too",
          not app.keys.lit and not app.keys.lights.snapshot(),
          f"lit={sorted(app.keys.lit)}")

    # The click must never end up doing nothing: the menu is the only route to
    # Quit, so every failure has to fall back to it.
    app._close_panel()
    panel.fail_open = True
    status_item.popped.clear()
    handler.statusItemClicked_(button)
    check("a popover that refuses to appear falls back to the menu",
          status_item.popped == [app.menu._menu] and not panel.is_open
          and not app.panel_timer.started,
          f"popped={len(status_item.popped)} timer={app.panel_timer.started}")
    panel.fail_open = False
    saved_panel, app.panel = app.panel, None
    status_item.popped.clear()
    handler.statusItemClicked_(button)
    check("with no panel at all the click still drops the menu",
          status_item.popped == [app.menu._menu],
          f"popped={len(status_item.popped)}")
    app.panel = saved_panel

    keyboardview._make_click_handler = lambda on_click: None
    probe_item = _NSStatusItem()
    probe_item.setMenu_(app.menu._menu)
    installed = keyboardview.intercept_status_item(probe_item,
                                                  lambda secondary: None)
    check("an interception that cannot be wired leaves rumps' menu in place",
          installed is None and probe_item.menu() is app.menu._menu
          and probe_item.button().action is None,
          f"{installed!r} action={probe_item.button().action!r}")
    keyboardview._make_click_handler = fake_click_handler
    check("popping up a menu that is not there is refused, not crashed",
          keyboardview.pop_up_menu(status_item, None) is False
          and keyboardview.pop_up_menu(None, app.menu._menu) is False)

    # ... and the tray must not think it has a panel in that case: the menu is
    # still on the status item, so a panel armed as well would leave two owners
    # fighting over one click.
    app._close_panel()
    keyboardview._make_click_handler = lambda on_click: None
    app._install_panel()
    check("a tray that could not take the click does not arm a panel either",
          app.panel is None and app._click_handler is None,
          f"panel={app.panel!r} handler={app._click_handler!r}")
    keyboardview._make_click_handler = fake_click_handler
    app._install_panel()
    panel, handler = app.panel, app._click_handler
    check("re-installing over a wired click gives the keyboard back",
          panel is not None and handler is not None
          and status_item.menu() is None)

    print("\n[16b] the real AppKit layer, on a fake Objective-C runtime")
    # Everything above ran the *no-pyobjc* branches of `keyboardview` with a
    # `_FakePanel` in place of the popover, which left the whole AppKit half of
    # that module unexecuted: an audit renamed `KeyboardPanel.refresh_mute` and
    # neutered `showRelativeToRect:ofView:preferredEdge:` and the suite still
    # reported 0 failed. So the real classes are built here against the fake
    # Objective-C runtime at the top of this file, and what is asserted is the
    # part Python cannot check for itself: the *selector names* and the
    # *argument types* that cross into ObjC. A renamed method, a mistyped
    # selector or the wrong number of colons is not an ImportError - it is
    # `doesNotRecognizeSelector:` the first time somebody clicks the icon.
    import gc
    import inspect

    from justpiano import keyboard as kb_mod

    real_panel = None
    saved_fake_panel = app.panel
    ak, restore_appkit = _install_fake_appkit()
    keyboardview._make_click_handler = real_make_click_handler
    try:
        classes = keyboardview._build_classes()
        built = sorted(n for n in _OBJC_CLASSES if n.startswith("JustPiano"))
        check("with pyobjc importable the AppKit half builds its ObjC classes",
              keyboardview.available() is True and classes is not None
              and issubclass(classes.KeyboardView, ak.NSView)
              and built == ["JustPianoActionTarget", "JustPianoKeyboardView",
                            "JustPianoMenuPopper", "JustPianoPopoverDelegate"],
              str(built))

        # The selector table: every method AppKit is told to send, spelled the way
        # it is spelled in `setAction_`/`performSelector:`/the delegate protocol.
        expected_selectors = {
            classes.KeyboardView: ("isFlipped", "acceptsFirstMouse:", "drawRect:",
                                   "mouseDown:", "mouseDragged:", "mouseUp:"),
            classes.ActionTarget: ("statusItemClicked:", "settingsClicked:",
                                   "muteClicked:"),
            classes.PopoverDelegate: ("popoverDidClose:",),
            classes.MenuPopper: ("popUpNow:",),
        }
        missing = [f"-[{cls.__name__} {sel}]"
                   for cls, sels in expected_selectors.items() for sel in sels
                   if not cls.alloc().respondsToSelector_(sel)]
        check("every selector the app installs is actually implemented",
              not missing, str(missing))
        wrong_arity = []
        for cls, sels in expected_selectors.items():
            for sel in sels:
                method = getattr(cls, sel.replace(":", "_"), None)
                if method is None:
                    continue
                takes = len(inspect.signature(method).parameters) - 1   # minus self
                if takes != sel.count(":"):
                    wrong_arity.append(f"-[{cls.__name__} {sel}] takes {takes}")
        check("...and takes exactly as many arguments as its selector has colons",
              not wrong_arity, str(wrong_arity))

        # ---- the panel itself, built by the real create_panel().
        real_panel = real_create_panel(app.keys, status_item)
        app.panel = real_panel
        popover = real_panel._popover
        content = popover.contentViewController().view()
        view = real_panel._view
        board = app.keys.keyboard
        margin, header = keyboardview.PANEL_MARGIN, keyboardview.HEADER_HEIGHT
        check("create_panel builds a popover with the 88-key view inside it",
              real_panel is not None and isinstance(popover, _NSPopoverStub)
              and view in content.subviews()
              and view.frame() == _NSRect(margin, margin, board.width, board.height)
              and popover.contentSize() == _NSSize(board.width + 2 * margin,
                                                   board.height + header + 2 * margin),
              f"content={popover.contentSize()!r} view={view.frame()!r}")
        check("the popover is transient, so AppKit still owns the dismissal",
              popover.behavior() == ak.NSPopoverBehaviorTransient
              and popover.animates() is False,
              f"behavior={popover.behavior()} animates={popover.animates()}")
        check("the keyboard view is flipped and takes the first click in the popover",
              view.isFlipped() is True and view.acceptsFirstMouse_(None) is True,
              f"flipped={view.isFlipped()}")

        buttons = {b.action: b for b in content.subviews()
                   if isinstance(b, _NSButtonStub)}
        target = real_panel._gear_target
        check("the gear and mute buttons send selectors their target implements",
              sorted(buttons) == ["muteClicked:", "settingsClicked:"]
              and buttons["muteClicked:"] is real_panel._mute_button
              and all(b.target is target for b in buttons.values())
              and all(target.respondsToSelector_(a) for a in buttons)
              and all(b.bordered is False for b in buttons.values()),
              str(sorted(buttons)))
        check("the gear says what it is, for anybody who cannot see a symbol",
              buttons["settingsClicked:"].tooltip == "Settings"
              and buttons["settingsClicked:"].image().symbol == "gearshape.fill",
              f"{buttons['settingsClicked:'].tooltip!r}")

        # ---- open(): the one call that puts it on screen.
        nsapp = ak.NSApplication.sharedApplication()
        nsapp.activations.clear()
        popover.shows.clear()
        opened = real_panel.open()
        rect, anchor, edge = popover.shows[-1] if popover.shows else (None, None, None)
        check("open() shows the popover relative to the status item button",
              opened is True and real_panel.is_open is True
              and len(popover.shows) == 1 and anchor is status_item.button()
              and isinstance(rect, _NSRect)
              and rect == status_item.button().bounds()
              and edge == ak.NSRectEdgeMinY,
              f"opened={opened} shows={len(popover.shows)} rect={rect!r} edge={edge!r}")
        check("opening it brings this accessory app forward, or it takes no clicks",
              nsapp.activations == ["activate"], str(nsapp.activations))
        real_panel.close()
        _ak_legacy_activation(True)         # macOS 13: NSApplication has no -activate
        nsapp.activations.clear()
        real_panel.open()
        check("on a Mac without -activate the older selector is used instead",
              nsapp.activations == ["activateIgnoringOtherApps:"]
              and real_panel.is_open, str(nsapp.activations))
        _ak_legacy_activation(False)

        # ---- drawRect:, through the real NSBezierPath/NSAttributedString calls.
        app.keys.lights.release_all()
        app.keys.refresh()
        _AK.painted.clear()
        view.drawRect_(view.bounds())
        painted = list(_AK.painted)
        keys_painted = [p for p in painted if p[0] == "fill"]
        labels = [p for p in painted if p[0] == "text"]
        check("drawRect: fills the background, all 88 keys and the octave labels",
              painted and painted[0][0] == "fillRect" and len(keys_painted) == 88
              and len(labels) == len(kb_mod.LABEL_NOTES)
              and any(p[1] == "C4" for p in labels),
              f"{len(keys_painted)} keys, {len(labels)} labels")
        c_rect = board.rect(60)
        app.keys.lights.press(60, 120)
        app.keys.refresh()
        _AK.painted.clear()
        view.drawRect_(view.bounds())
        lit_fill = [p[2].rgba for p in _AK.painted
                    if p[0] == "fill" and abs(p[1].origin.x - c_rect.x) < 1e-6]
        check("a key that is down is painted in its velocity colour, not plain white",
              lit_fill and lit_fill[0][:3] != tuple(float(v) for v in kb_mod.WHITE_FILL),
              f"{lit_fill[:1]}")
        app.keys.lights.release_all()
        app.keys.refresh()
        requests = view.display_requests
        real_panel.redraw()
        check("redraw() marks the view dirty instead of painting on the spot",
              view.display_requests == requests + 1,
              f"{view.display_requests - requests} setNeedsDisplay:")

        # ---- the three mouse selectors, on the real view.
        front = _NSPoint(margin + c_rect.center_x, margin + 1.0)
        view.mouseDown_(_ObjCEvent(ak.NSEventTypeLeftMouseDown, location=front))
        loud = app.keys.lights.snapshot().get(60)
        check("mouseDown: plays the key under the pointer",
              app.keys.mouse_note == 60 and loud is not None and loud > 110,
              f"note={app.keys.mouse_note} velocity={loud}")
        d_rect = board.rect(62)
        view.mouseDragged_(_ObjCEvent(
            ak.NSEventTypeLeftMouseDown,
            location=_NSPoint(margin + d_rect.center_x, margin + 1.0)))
        check("mouseDragged: glissandos instead of stacking notes",
              app.keys.mouse_note == 62
              and 60 not in app.keys.lights.snapshot(),
              f"note={app.keys.mouse_note} down={sorted(app.keys.lights.snapshot())}")
        view.mouseUp_(_ObjCEvent(ak.NSEventTypeLeftMouseUp))
        check("mouseUp: lets go of it",
              app.keys.mouse_note is None and not app.keys.lights.snapshot(),
              f"down={sorted(app.keys.lights.snapshot())}")
        # The flipped view is what makes "low on the key" mean "hard": a view that
        # stopped being flipped would turn every click into the wrong velocity and,
        # among the black keys, the wrong note.
        view.mouseDown_(_ObjCEvent(
            ak.NSEventTypeLeftMouseDown,
            location=_NSPoint(margin + c_rect.center_x, margin + board.height - 1.0)))
        soft = app.keys.lights.snapshot().get(60)
        view.mouseUp_(_ObjCEvent(ak.NSEventTypeLeftMouseUp))
        check("...and the flipped coordinates put the quiet end at the top of the key",
              soft is not None and soft < 60 and loud - soft > 40,
              f"{soft} at the top vs {loud} at the front")

        # ---- popoverDidClose:, the delegate the whole click dance rests on.
        check("the popover has a delegate for popoverDidClose:, and keeps it alive",
              real_panel._delegate is not None
              and popover.delegate() is real_panel._delegate
              and popover.delegate().respondsToSelector_("popoverDidClose:"),
              repr(popover.delegate()))

        real_handler = keyboardview.intercept_status_item(status_item,
                                                         app._status_item_clicked)
        app._click_handler = real_handler
        real_button = status_item.button()
        check("intercept_status_item takes the menu off the item and wires the button",
              real_handler is not None and status_item.menu() is None
              and real_button.target is real_handler
              and real_button.action == "statusItemClicked:"
              and real_button.action_mask == keyboardview.CLICK_MASK,
              f"action={real_button.action!r} mask={real_button.action_mask}")
        throwaway = keyboardview._make_click_handler(lambda secondary: None)
        real_button.setTarget_(throwaway)
        del throwaway
        gc.collect()
        check("AppKit holds the target weakly, which is why the tray keeps a reference",
              real_button.target is None
              and real_button.performClick_(real_button) is None,
              f"target={real_button.target!r}")
        real_button.setTarget_(real_handler)

        icon_frame = real_button.window().frame()
        on_icon = _NSPoint(icon_frame.origin.x + 3.0, icon_frame.origin.y + 3.0)

        def click_icon(kind=None, flags=0):
            """Deliver a click the way AppKit does: the action, on mouse-*up*."""
            nsapp.current_event = _ObjCEvent(
                ak.NSEventTypeLeftMouseUp if kind is None else kind, flags=flags)
            real_button.performClick_(real_button)

        def dismiss_where(point, kind=None):
            """A transient dismissal, caused by the mouse-*down* at `point`."""
            _AK.mouse_location = point
            nsapp.current_event = _ObjCEvent(
                ak.NSEventTypeLeftMouseDown if kind is None else kind)
            popover.dismiss_transient()

        real_panel.close()
        popover.shows.clear()
        click_icon()
        check("a left click on the icon opens the real popover",
              real_panel.is_open and len(popover.shows) == 1
              and app.panel_timer.started, f"shows={len(popover.shows)}")
        dismiss_where(on_icon)
        stamped = real_panel.dismissed_at
        click_icon()
        check("the click that dismissed the popover does not reopen it",
              stamped is not None and not real_panel.is_open
              and len(popover.shows) == 1 and real_panel.dismissed_at is None
              and not app.panel_timer.started,
              f"stamp={stamped} shows={len(popover.shows)}")
        click_icon()
        check("...and the click after it opens the keyboard again",
              real_panel.is_open and len(popover.shows) == 2,
              f"shows={len(popover.shows)}")
        dismiss_where(_NSPoint(20.0, 20.0))     # a click somewhere else entirely
        check("a dismissal caused by a click elsewhere is not stamped",
              not real_panel.is_open and real_panel.dismissed_at is None,
              f"stamp={real_panel.dismissed_at}")
        click_icon()
        check("...so that click on the icon is a plain request to open it",
              real_panel.is_open and len(popover.shows) == 3,
              f"shows={len(popover.shows)}")
        dismiss_where(on_icon, kind=ak.NSEventTypeKeyDown)
        check("Escape closing the popover is a known non-click, not a stamp",
              not real_panel.is_open and real_panel.dismissed_at is None,
              f"stamp={real_panel.dismissed_at}")
        click_icon()
        _AK.mouse_location = on_icon
        nsapp.current_event = _ObjCEvent(ak.NSEventTypeLeftMouseDown)
        real_panel.close()                      # a close *we* asked for
        check("a close the app asked for is not a user dismissal, mouse or no mouse",
              not real_panel.is_open and real_panel.dismissed_at is None,
              f"stamp={real_panel.dismissed_at}")
        click_icon()
        check("...so the icon still works after the app closed the keyboard itself",
              real_panel.is_open)

        # ---- the menu, popped from the next pass of the run loop.
        status_item.popped.clear()
        _RUNLOOP.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        click_icon(ak.NSEventTypeRightMouseUp)
        check("a right click is not run inside the click's own event handling",
              status_item.popped == [] and len(_RUNLOOP) == 1
              and not real_panel.is_open, f"queued={len(_RUNLOOP)}")
        check("...it pops the menu on the next pass of the run loop",
              _pump_runloop() == 1 and status_item.popped == [app.menu._menu],
              f"popped={len(status_item.popped)}")
        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        click_icon(flags=ak.NSEventModifierFlagControl)
        _pump_runloop()
        check("a control-click counts as a right click, the way macOS says",
              status_item.popped == [app.menu._menu] and not real_panel.is_open,
              f"popped={len(status_item.popped)}")

        click_icon()
        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        buttons["settingsClicked:"].performClick_(buttons["settingsClicked:"])
        check("the gear's selector reaches the tray and puts the keyboard away first",
              not real_panel.is_open and status_item.popped == []
              and len(_RUNLOOP) == 1, f"queued={len(_RUNLOOP)}")
        _pump_runloop()
        check("...and the menu opens once that click has been dealt with",
              status_item.popped == [app.menu._menu],
              f"popped={len(status_item.popped)}")

        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        _RUNLOOP.clear()
        keyboardview.pop_up_menu(status_item, app.menu._menu)
        keyboardview.pop_up_menu(status_item, app.menu._menu)
        check("two pop-ups in one event coalesce into one",
              len(_RUNLOOP) == 1 and _pump_runloop() == 1
              and status_item.popped == [app.menu._menu],
              f"popped={len(status_item.popped)}")
        status_item.popped.clear()
        # A stamp older than POP_UP_COALESCE means the run loop never came back
        # for it; coalescing onto that forever makes the menu unreachable, and the
        # menu is the only way to Quit.
        keyboardview._POP_UP_QUEUED_AT = (time.monotonic()
                                          - keyboardview.POP_UP_COALESCE - 0.1)
        keyboardview.pop_up_menu(status_item, app.menu._menu)
        _pump_runloop()
        check("a queued pop-up the run loop never ran does not lose the menu",
              status_item.popped == [app.menu._menu],
              f"popped={len(status_item.popped)}")

        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        nested = []
        status_item.while_popping = lambda: nested.append(
            keyboardview._pop_up_now(status_item, app.menu._menu))
        keyboardview.pop_up_menu(status_item, app.menu._menu)
        _pump_runloop()
        check("a pop-up from inside popUpStatusItemMenu:'s tracking loop is refused",
              nested == [False] and status_item.popped == [app.menu._menu]
              and keyboardview._POPPING is False,
              f"nested={nested} popped={len(status_item.popped)}")

        status_item.fail_pop_up = True
        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        with quiet_stderr() as noise:
            keyboardview.pop_up_menu(status_item, app.menu._menu)
            _pump_runloop()
        check("a popUpStatusItemMenu: that has gone falls back to rumps' own menu",
              status_item.menu() is app.menu._menu
              and "popUpStatusItemMenu" in noise.getvalue(),
              f"menu={status_item.menu()!r}")
        status_item.fail_pop_up = False
        status_item.setMenu_(None)              # back to the intercepted arrangement

        # ---- refresh_mute: the call `tray._refresh_mute_state` makes on every
        # toggle. Renaming it is an AttributeError on every real mute click, and
        # was invisible here until the real panel was under the tray.
        mute_button = real_panel._mute_button
        raised = None
        try:
            app.set_muted(True)
        except Exception as exc:
            raised = exc
        image = mute_button.image()
        check("the tray's mute refresh reaches the real panel's mute button",
              raised is None and app.keys.muted is True and image is not None
              and image.symbol == keyboardview.MUTE_SYMBOLS[True]
              and mute_button.title() == "" and mute_button.tooltip == "Unmute",
              f"{raised!r} image={image!r} tip={mute_button.tooltip!r}")
        app.set_muted(False)
        check("...and swaps back to the audible symbol when unmuted",
              mute_button.image() is not None
              and mute_button.image().symbol == keyboardview.MUTE_SYMBOLS[False]
              and mute_button.tooltip == "Mute",
              f"image={mute_button.image()!r}")
        _AK.sf_symbols = False                  # macOS 10.x: no SF Symbols at all
        app.set_muted(True)
        check("without SF Symbols the mute button falls back to a text glyph",
              mute_button.image() is None
              and mute_button.title() == keyboardview.MUTE_GLYPHS[True]
              and mute_button.tooltip == "Unmute",
              f"title={mute_button.title()!r}")
        app.set_muted(False)
        legacy_panel = real_create_panel(app.keys, status_item)
        legacy_gear = [b for b in legacy_panel._popover.contentViewController()
                       .view().subviews()
                       if isinstance(b, _NSButtonStub)
                       and b.action == "settingsClicked:"][0]
        check("...and so does the gear, rather than being a blank button",
              legacy_gear.image() is None and legacy_gear.title() == "\u2699"
              and legacy_panel._mute_button.title()
              == keyboardview.MUTE_GLYPHS[False],
              f"gear={legacy_gear.title()!r}")
        _AK.sf_symbols = True

        # ---- and the failure the popover really has: no window to anchor to.
        real_panel.close()
        popover.fail_show = True
        with quiet_stderr() as noise:
            refused = real_panel.open()
        check("a popover AppKit refuses to show is reported, not raised",
              refused is False and real_panel.is_open is False
              and "popover" in noise.getvalue(),
              f"{refused!r}")
        status_item.popped.clear()
        keyboardview._POP_UP_QUEUED_AT = None
        with quiet_stderr():
            click_icon()
            _pump_runloop()
        check("a keyboard that cannot appear leaves the user the menu",
              status_item.popped == [app.menu._menu]
              and not app.panel_timer.started,
              f"popped={len(status_item.popped)}")
        popover.fail_show = False
        status_item.setMenu_(None)

        # ObjC can only register a class name once per process, which is why
        # `_build_classes()` caches its result: clearing that cache and asking
        # again is a duplicate-class error, not a second set of classes.
        cached = (keyboardview._CLASSES, keyboardview._TRIED)
        keyboardview._CLASSES, keyboardview._TRIED = None, False
        raised = None
        try:
            keyboardview._build_classes()
        except Exception as exc:
            raised = exc
        keyboardview._CLASSES, keyboardview._TRIED = cached
        check("the ObjC classes are built once, because a name can only exist once",
              isinstance(raised, _ObjCError), repr(raised))
    finally:
        if real_panel is not None:
            real_panel.close()
        app.panel_timer.stop()
        app.panel = saved_fake_panel
        keyboardview._make_click_handler = fake_click_handler
        _AK.sf_symbols = True
        restore_appkit()

    # Back to the stand-in for the rest of the run: [17]-[19] are about the tray,
    # and on this machine there is no pyobjc.
    status_item.setMenu_(None)
    app._install_panel()
    panel, handler = app.panel, app._click_handler
    check("the suite is back on the stand-in panel, with the app unmuted",
          isinstance(panel, _FakePanel) and isinstance(handler, _FakeClickHandler)
          and button.target is handler and status_item.menu() is None
          and app.engine.muted is False and not app.panel_timer.started,
          f"panel={type(panel).__name__} muted={app.engine.muted}")

    print("\n[17] mute")
    from justpiano import hotkey as hk

    # The app's *live* stream, not `_STREAMS[-1]`: [15] relaunches the app to
    # check the remembered voicing and quits it again, so the last stream the
    # stub ever opened is that dead one. Reading blocks off a stopped stream
    # would make "muted is silent" pass because nothing was ever rendered, and
    # "unmuting brings the sound back" fail for the same reason - the audio
    # assertions below have to be tied to the stream this app is playing on.
    stream = app.engine.stream
    streams_before = len(_STREAMS)
    titles_before = len(status_item.titles)
    check("precondition: the engine is playing on a stream the stub is driving",
          stream is not None and stream in _STREAMS and stream._thread is not None
          and not stream._stop.is_set(),
          f"stream {_STREAMS.index(stream) if stream in _STREAMS else None} "
          f"of {len(_STREAMS)}")
    check("precondition: nothing is muted and the icon is the plain piano",
          app.engine.muted is False and app.icon is None
          and status_item.image() is None and app.mute_item.state == 0
          and app.keys.muted is False and panel.muted is False,
          f"muted={app.engine.muted} icon={app.icon!r}")
    check("asset_path finds the shipped image, and only images that exist",
          tray_mod.asset_path(tray_mod.MUTED_ICON_FILE) is not None
          and tray_mod.asset_path("not-an-asset.png") is None,
          str(tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)))

    # Frozen: PyInstaller unpacks the spec's `datas` and points `sys._MEIPASS` at
    # them, so a .app has to find the image there - the source tree it was built
    # from is not shipped. The bundle itself can only be built on a Mac, so the
    # two layouts PyInstaller 6 produces are faked here instead: the assets under
    # `Contents/Frameworks` (which is what `_MEIPASS` is) and the same files under
    # `Contents/Resources`, which is where they physically live.
    frozen = os.path.join(tmpdir, "JustPiano.app", "Contents")
    for where in ("Frameworks", "Resources"):
        os.makedirs(os.path.join(frozen, where, "assets"), exist_ok=True)
    linked = os.path.join(frozen, "Frameworks", "assets", tray_mod.MUTED_ICON_FILE)
    shipped = os.path.join(frozen, "Resources", "assets", tray_mod.MUTED_ICON_FILE)
    for path in (linked, shipped):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
    sys._MEIPASS = os.path.join(frozen, "Frameworks")
    try:
        found = tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)
        absent = tray_mod.asset_path("not-an-asset.png")
        os.remove(linked)               # no cross-link: only Contents/Resources
        fallback = tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)
    finally:
        del sys._MEIPASS
    check("a frozen .app finds the image PyInstaller bundled next to the code",
          found == linked and absent is None, repr(found))
    check("...and finds it in Contents/Resources even without the cross-link",
          fallback == shipped, repr(fallback))
    check("...while a source checkout still resolves to its own assets directory",
          tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)
          == os.path.join(os.path.dirname(os.path.dirname(
              os.path.abspath(tray_mod.__file__))), "assets",
              tray_mod.MUTED_ICON_FILE),
          str(tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)))

    # ---- entry point 1 of 3: the menu item.
    refreshes = panel.mute_refreshes
    app.mute_item.click()
    check("one click on the menu item mutes the engine",
          app.engine.muted is True and app.engine._pending_mute is True,
          f"pending={app.engine._pending_mute}")
    check("the menu item checkmarks itself and says what state it is in",
          app.mute_item.state == 1 and app.mute_item.title == "Muted",
          f"{app.mute_item.title!r} state={app.mute_item.state}")
    check("the panel's button is told, and reads the state off the controller",
          app.keys.muted is True and panel.muted is True
          and panel.mute_refreshes == refreshes + 1,
          f"controller={app.keys.muted} button={panel.muted}")
    image = status_item.image()
    check("the menu bar icon swaps to the muted image",
          image is not None
          and os.path.basename(image.path) == tray_mod.MUTED_ICON_FILE,
          repr(image.path if image else None))
    check("...as a template image, so it inverts on a dark menu bar as the emoji did",
          image is not None and image.template is True and app.template is True,
          f"template={image.template if image else None}")
    check("the image replaces the piano glyph instead of doubling it",
          app.title == "" and status_item.title() == "", repr(app.title))
    check("and the menu bar never flashed the app name during the swap",
          "Just Piano" not in status_item.titles[titles_before:],
          str(status_item.titles[titles_before:]))

    # ---- muted means the output stage, and nothing else: MIDI, the recorder and
    # the key lights all have to carry on.
    check("the fade runs to completion, so the output is exactly silent",
          wait_until(lambda: app.engine.mute_gain == 0.0, 5.0, 0.02),
          f"gain={app.engine.mute_gain}")
    app.record_item.click()
    app._open_panel()
    stream.captured.clear()
    delivered = all(_FakeMidiIn.send(NOTE_ON, note, 100) for note in (60, 64, 67))
    wait_rendered(stream, 0.25)
    silent = np.concatenate(stream.captured) if stream.captured else np.ones((1, 2))
    check("keys played while muted make no sound at all",
          delivered and float(np.abs(silent).max()) == 0.0
          and app.engine.active_voices == 0,
          f"peak={float(np.abs(silent).max()):.2e} "
          f"voices={app.engine.active_voices}")
    app.panel_timer.fire()
    check("but they still light up on the on-screen keyboard",
          sorted(app.keys.lit) == [60, 64, 67], f"lit={sorted(app.keys.lit)}")
    app.timer.fire()
    check("the record timer keeps the menu bar even while the muted icon is up",
          app.title.startswith(ICON_REC) and status_item.image() is not None,
          repr(app.title))
    for note in (60, 64, 67):
        _FakeMidiIn.send(NOTE_OFF, note, 0)
    app.record_item.click()
    app.timer.fire()
    check("the take recorded in silence has every note in it",
          "3 notes" in app.take_item.title, app.take_item.title)
    app.save_midi_item.click()
    quiet_take = mido.MidiFile(saved_paths[-1])
    check("...and it exports like any other take",
          len([m for tr in quiet_take.tracks for m in tr
               if m.type == "note_on" and m.velocity > 0]) == 3,
          os.path.basename(saved_paths[-1]))
    check("the status line says it is muted and that recording still works",
          "\U0001F507" in app.status_item.title
          and tray_mod.ICON_MUTED == "\U0001F507"
          and "Muted" in app.status_item.title
          and "recording" in app.status_item.title, app.status_item.title)

    # ---- entry point 2 of 3: the panel's mute button.
    app.keys.mute_clicked()
    check("the panel's button unmutes, and every other entry point follows it",
          app.engine.muted is False and app.mute_item.state == 0
          and app.mute_item.title == "Mute" and app.keys.muted is False
          and panel.muted is False and status_item.image() is None
          and app.icon is None,
          f"muted={app.engine.muted} state={app.mute_item.state}")
    check("the menu bar goes back to a glyph rather than the app's name",
          status_item.title() in (ICON_IDLE, tray_mod.ICON_NOTE)
          and "Just Piano" not in status_item.titles[titles_before:],
          repr(status_item.title()))
    stream.captured.clear()
    _FakeMidiIn.send(NOTE_ON, 60, 100)
    wait_rendered(stream, 0.3)
    audible = np.concatenate(stream.captured) if stream.captured else np.zeros((1, 2))
    check("unmuting brings the sound straight back, on the very same stream",
          rms(audible) > 1e-3 and len(_STREAMS) == streams_before
          and app.engine.stream is stream and app.engine.error is None,
          f"rms={rms(audible):.4f} streams={len(_STREAMS)}/{streams_before}")
    _FakeMidiIn.send(NOTE_OFF, 60, 0)
    app.menu["Panic (All Notes Off)"].click()

    # A source checkout that never ran tools/make_icon.py: rumps builds the
    # NSImage eagerly and raises, and that must cost the image, not the app.
    app._muted_icon = os.path.join(tmpdir, "not-rendered-yet.png")
    raised = None
    try:
        app.mute_item.click()
    except Exception as exc:
        raised = exc
    check("a muted image that is not there falls back to a glyph, not a crash",
          raised is None and app.engine.muted is True and app.icon is None
          and status_item.image() is None
          and app.title.startswith("\U0001F507")
          and "Just Piano" not in status_item.titles[titles_before:],
          f"{raised!r} title={app.title!r}")
    app.timer.fire()
    check("...and the fallback survives the next tick too",
          app.title.startswith("\U0001F507") and app.mute_item.state == 1
          and "Just Piano" not in status_item.titles[titles_before:],
          repr(app.title))
    app._muted_icon = tray_mod.asset_path(tray_mod.MUTED_ICON_FILE)
    app.mute_item.click()

    # Toggling with no keyboard panel (it is closed most of the time) must not
    # reach for one, and the panel must be right again when it comes back.
    saved_panel, app.panel = app.panel, None
    raised = None
    try:
        app.mute_item.click()
    except Exception as exc:
        raised = exc
    check("muting with no panel on screen is not a crash",
          raised is None and app.engine.muted is True and app.keys.muted is True,
          f"{raised!r}")
    app.panel = saved_panel
    app.mute_item.click()
    check("and the panel's button is back in step afterwards",
          app.engine.muted is False and panel.muted is False
          and app.keys.muted is False)

    # Mute is deliberately not persisted: an app that launches silent looks
    # broken. The hotkey preference is (see [18]).
    app.mute_item.click()
    relaunched = JustPianoApp()
    try:
        check("a fresh launch is never silent, whatever was muted before it",
              app.engine.muted is True and relaunched.engine.muted is False
              and relaunched.mute_item.state == 0 and relaunched.icon is None
              and "muted" not in DEFAULTS,
              f"old={app.engine.muted} new={relaunched.engine.muted}")
    finally:
        relaunched._quit(None)
    app.mute_item.click()
    check("back to audible for the rest of the run",
          app.engine.muted is False and app.mute_item.state == 0)

    print("\n[18] mute hotkey")
    # The submenu is the presets, then a separator, then the Accessibility line
    # that the rest of this section drives - so comparing *every* entry against
    # the preset list alone (what this check used to do) could not pass on any
    # correct build. Spell the whole shape out in order instead: a lost or
    # re-ordered preset, a label rumps would have collapsed as a duplicate, a
    # preset left without a callback, a missing separator and a stray extra entry
    # all still fail.
    #
    # `list(menu)` is the keys, and rumps keys an item by the title it had when
    # it was added and never rekeys it (rumps.py:376-388) - hence the *initial*
    # Accessibility title here, whatever it says by now.
    listed = list(app.hotkey_menu)
    presets = [tray_mod.HOTKEY_OFF_LABEL] + [hk.label(s)
                                             for s in tray_mod.HOTKEY_SPECS if s]
    expected = presets + ["SeparatorMenuItem_1", tray_mod.ACCESS_TITLE_OFF]
    check("the submenu lists exactly the presets, spelled the way macOS spells them",
          listed == expected
          and all(app.hotkey_menu[t].callback is not None for t in presets)
          and app.hotkey_menu[tray_mod.ACCESS_TITLE_OFF] is app.access_item,
          str(listed))
    check("the persisted hotkey is the armed one and the checked one",
          app.hotkeys.hotkey == hk.parse(app.settings["mute_hotkey"])
          and app.hotkey_menu[hk.label(app.settings["mute_hotkey"])].state == 1
          and app.hotkey_menu[tray_mod.HOTKEY_OFF_LABEL].state == 0,
          f"{app.settings['mute_hotkey']} / {app.hotkeys.hotkey}")
    check("with no pyobjc at all the menu says so instead of blaming the user",
          hk.available() is False
          and app.access_item.title == tray_mod.ACCESS_TITLE_UNAVAILABLE
          and app.access_item.callback is None, app.access_item.title)

    # From here on pyobjc "exists": the NSEvent stub is faithful about which
    # monitor sees which event, which is the whole point of installing two.
    nsevent, restore_nsevent = _install_nsevent(hk, trusted=False)
    mods = hk.CONTROL | hk.OPTION | hk.COMMAND
    press = _KeyEvent(hk.KEY_CODES["m"], mods)
    app._apply_hotkey(app.settings["mute_hotkey"])
    check("with Accessibility denied only the local monitor is installed",
          nsevent.kinds == ["local"] and app.hotkeys.local_installed
          and not app.hotkeys.global_installed and nsevent.installed == ["local"],
          f"{nsevent.installed}")
    check("the menu offers the Privacy pane instead of failing silently",
          app.access_item.title == tray_mod.ACCESS_TITLE_DENIED
          and app.access_item.callback is not None, app.access_item.title)
    opened: list = []
    real_open_url = macui.open_url
    macui.open_url = lambda url: opened.append(url)
    app.access_item.click()
    macui.open_url = real_open_url
    check("clicking it opens the Accessibility pane, and never prompts by itself",
          opened == [hk.ACCESSIBILITY_PANE]
          and "Privacy_Accessibility" in opened[0], str(opened))

    # ---- entry point 3 of 3: the hotkey.
    check("the hotkey works while Just Piano is focused, permission or not",
          nsevent.post(press, scope="local") == 1 and app.engine.muted is True
          and app.mute_item.state == 1 and app.keys.muted is True
          and panel.muted is True and status_item.image() is not None,
          f"muted={app.engine.muted} state={app.mute_item.state}")
    check("...and the missing permission is exactly the 'other app has focus' half",
          nsevent.post(press, scope="global") == 0 and app.engine.muted is True,
          f"muted={app.engine.muted}")
    installs = list(nsevent.installed)
    for _ in range(4):
        app.timer.fire()
    check("a permission that stays denied is not re-attempted twice a second",
          nsevent.installed == installs
          and app.access_item.title == tray_mod.ACCESS_TITLE_DENIED,
          f"{nsevent.installed}")

    _set_trusted(hk, True)
    app.timer.fire()
    check("granting Accessibility while the app runs arms the global half",
          app.hotkeys.global_installed and nsevent.kinds == ["global", "local"]
          and app.access_item.title == tray_mod.ACCESS_TITLE_OK
          and app.access_item.callback is None,
          f"{nsevent.kinds} / {app.access_item.title}")
    check("the local monitor was replaced, not stacked on top of itself",
          len(nsevent.tokens) == 2 and len(nsevent.removed) == 1,
          f"{len(nsevent.tokens)} live, {len(nsevent.removed)} removed")
    tokens = dict(nsevent.tokens)
    for _ in range(4):
        app.timer.fire()
    check("and it is not re-armed on every tick from then on",
          nsevent.tokens == tokens, f"{len(nsevent.tokens)} monitors")

    check("with trust granted the hotkey fires while a DAW has focus",
          nsevent.post(press, scope="global") == 1 and app.engine.muted is False
          and app.mute_item.state == 0 and status_item.image() is None,
          f"muted={app.engine.muted}")
    check("and the app's own monitor does not toggle it twice",
          nsevent.post(press, scope="local") == 1 and app.engine.muted is True,
          f"muted={app.engine.muted}")
    check("holding the hotkey down does not stutter mute on and off",
          nsevent.post(_KeyEvent(hk.KEY_CODES["m"], mods, repeat=True),
                       scope="global") == 1 and app.engine.muted is True)
    check("somebody else's shortcut passing under the monitor is left alone",
          nsevent.post(_KeyEvent(hk.KEY_CODES["m"], hk.COMMAND), "global") == 1
          and nsevent.post(_KeyEvent(hk.KEY_CODES["s"], mods), "global") == 1
          and nsevent.post(_KeyEvent(hk.KEY_CODES["m"], mods | hk.SHIFT),
                           "global") == 1
          and app.engine.muted is True, f"muted={app.engine.muted}")
    app.set_muted(False)

    app.hotkey_menu[hk.label("ctrl+alt+m")].click()
    check("picking another preset arms it, checks it and remembers it",
          app.settings["mute_hotkey"] == "ctrl+alt+m"
          and app.hotkeys.hotkey == hk.parse("ctrl+alt+m")
          and app.hotkey_menu[hk.label("ctrl+alt+m")].state == 1
          and app.hotkey_menu[hk.label("ctrl+alt+cmd+m")].state == 0,
          f"{app.settings['mute_hotkey']}")
    check("the monitors were rebuilt for it, and the old pair taken down",
          nsevent.kinds == ["global", "local"] and len(nsevent.tokens) == 2
          and len(nsevent.removed) == 3,
          f"{len(nsevent.tokens)} live, {len(nsevent.removed)} removed")
    check("the old shortcut stops working and the new one starts",
          nsevent.post(press, "global") == 1 and app.engine.muted is False
          and nsevent.post(_KeyEvent(hk.KEY_CODES["m"],
                                     hk.CONTROL | hk.OPTION), "global") == 1
          and app.engine.muted is True, f"muted={app.engine.muted}")
    check("the hotkey choice survives a re-read of settings.json",
          Settings()["mute_hotkey"] == "ctrl+alt+m"
          and DEFAULTS["mute_hotkey"] != "ctrl+alt+m",
          f"{Settings()['mute_hotkey']!r}")
    app.set_muted(False)

    app.hotkey_menu[tray_mod.HOTKEY_OFF_LABEL].click()
    check("Off disarms the hotkey and takes both monitors with it",
          app.settings["mute_hotkey"] is None and app.hotkeys.hotkey is None
          and not app.hotkeys.installed and nsevent.tokens == {}
          and app.access_item.title == tray_mod.ACCESS_TITLE_OFF
          and app.access_item.callback is None, app.access_item.title)
    check("with the hotkey off nothing can toggle mute behind the app's back",
          nsevent.post(press, "global") == 0
          and nsevent.post(press, "local") == 0
          and app.engine.muted is False)

    # Accessibility that cannot even be asked (the symbol would not bind) is not
    # a refusal: the global monitor is still worth installing.
    _set_trusted(hk, None)
    app.hotkey_menu[hk.label("f13")].click()
    check("an unknown Accessibility answer is not read as a denial",
          app.hotkeys.global_installed and app.hotkeys.local_installed
          and app.access_item.title == tray_mod.ACCESS_TITLE_UNKNOWN
          and app.access_item.callback is not None, app.access_item.title)
    check("a function-key preset matches even though macOS sets the fn flag",
          nsevent.post(_KeyEvent(hk.KEY_CODES["f13"], hk.FUNCTION), "global") == 1
          and app.engine.muted is True, f"muted={app.engine.muted}")
    app.set_muted(False)

    nsevent.fail_global = True
    app.hotkey_menu[hk.label("ctrl+alt+cmd+m")].click()
    check("a global monitor that AppKit refuses costs the global half only",
          app.hotkeys.local_installed and not app.hotkeys.global_installed
          and len(nsevent.tokens) == 1
          and app.hotkeys.hotkey == hk.parse("ctrl+alt+cmd+m"),
          f"{nsevent.kinds}")
    nsevent.fail_global = False
    _set_trusted(hk, True)
    app.timer.fire()
    check("...and is retried the next time Accessibility says yes",
          app.hotkeys.global_installed and nsevent.kinds == ["global", "local"],
          f"{nsevent.kinds}")
    before_spec = app.settings["mute_hotkey"]
    app._pick_hotkey(_MenuItem("Not A Preset"))
    check("a menu item that is not a preset cannot disarm the hotkey",
          app.settings["mute_hotkey"] == before_spec
          and app.hotkeys.hotkey is not None, repr(app.settings["mute_hotkey"]))

    # The retry above is deliberately *one* retry. `addGlobalMonitor...` is
    # documented as returning nil, and a permission problem that survives the
    # re-arm used to queue another attempt on every tick: an install twice a
    # second forever, each one taking the working local monitor down and back up
    # in between. `_apply_hotkey(rearm=False)` clears the flag whatever the
    # outcome, which is what this asserts.
    nsevent.fail_global = True
    _set_trusted(hk, False)
    app._apply_hotkey("ctrl+alt+cmd+m")
    check("precondition: armed with the global half missing and a re-arm pending",
          app._hotkey_rearm is True and not app.hotkeys.global_installed
          and app.hotkeys.local_installed,
          f"rearm={app._hotkey_rearm}")
    _set_trusted(hk, True)              # the user grants it, AppKit still says nil
    installs, removals = len(nsevent.installed), len(nsevent.removed)
    app.timer.fire()
    retried = len(nsevent.installed) - installs
    for _ in range(6):
        app.timer.fire()
    check("a global monitor that stays nil is retried once, not twice a second",
          retried == 2 and len(nsevent.installed) - installs == 2
          and app._hotkey_rearm is False,
          f"{len(nsevent.installed) - installs} installs over 7 ticks, "
          f"rearm={app._hotkey_rearm}")
    check("...and the local monitor that does work is left in place",
          app.hotkeys.local_installed and len(nsevent.tokens) == 1
          and len(nsevent.removed) - removals == 1,
          f"{nsevent.kinds} / {len(nsevent.removed) - removals} removed")
    check("the hotkey still works through that whole non-event",
          nsevent.post(_KeyEvent(46, 1835008), "local") == 1
          and app.engine.muted is True, f"muted={app.engine.muted}")
    app.mute_item.click()

    # And the asking itself is bounded. Polling a permission the user has decided
    # against was one AXIsProcessTrusted() call every half second for as long as
    # the app was up; TRUST_POLL_LIMIT turns that into a couple of minutes of
    # watching, and clicking the Accessibility line starts a fresh window.
    asked = []
    hk._AX_TRIED = True
    hk._AX_TRUSTED = lambda: asked.append(1) and False       # always falsy
    app._apply_hotkey("ctrl+alt+cmd+m")
    app._trust_polls = tray_mod.TRUST_POLL_LIMIT - 2
    asked.clear()
    for _ in range(6):
        app.timer.fire()
    check("asking macOS for the permission is bounded, not forever",
          len(asked) == 2 and app._trust_polls == tray_mod.TRUST_POLL_LIMIT
          and tray_mod.TRUST_POLL_LIMIT >= 60,
          f"{len(asked)} calls over 6 ticks, polls={app._trust_polls}")
    check("...and the line the user could act on is still there and clickable",
          app.access_item.title == tray_mod.ACCESS_TITLE_DENIED
          and app.access_item.callback is not None, app.access_item.title)
    asked.clear()
    app.access_item.click()             # "Allow Accessibility Access…"
    app.timer.fire()
    check("clicking it opens a fresh window, so a late grant is never unreachable",
          app._trust_polls == 1 and len(asked) == 1,
          f"polls={app._trust_polls} calls={len(asked)}")
    nsevent.fail_global = False
    _set_trusted(hk, True)
    app._apply_hotkey(before_spec)
    check("the hotkey is fully armed again for the shutdown checks",
          app.hotkeys.global_installed and app.hotkeys.local_installed
          and app._hotkey_rearm is False, f"{nsevent.kinds}")

    print("\n[19] shutdown")
    rumps.alerts.clear()
    app._busy = "rendering audio"
    app.menu["Quit"].click()
    check("quit is refused while a WAV export is in flight",
          app.engine.stream is not None and app.midi.port_name is not None
          and any("Please wait" in str(a) for a in rumps.alerts),
          f"stream={app.engine.stream is not None} alerts={rumps.alerts}")
    # The first refusal explains what quitting would cost, and a failed WAV export
    # leaves *nothing* behind (it renders to `<name>.part` and unlinks it), so the
    # wording is about a render that has to be done again - not a half-written file
    # to salvage.
    said = [str(a) for a in rumps.alerts if "Please wait" in str(a)]
    check("...and says the render is discarded, not that a broken file is left",
          said and "no file at all" in said[-1]
          and not any(word in said[-1] for word in ("unplayable", "truncated",
                                                    "half-written", "corrupt")),
          said[-1][:80] if said else "")
    # But Quit is the only way out of an app built with `quit_button=None`, and
    # `_busy` is cleared by the render thread's `finally` alone: a thread wedged on
    # a stalled volume would otherwise leave Force Quit as the only exit.
    _Window.answer = 0                      # "Keep Waiting"
    quits_before = len(_QUIT_CALLS)
    windows_before = len(_Window.opened)
    app.menu["Quit"].click()
    asked_again = _Window.opened[-1] if _Window.opened else None
    check("asking again offers the way out as a confirmation, not a second refusal",
          len(_Window.opened) == windows_before + 1 and asked_again is not None
          and asked_again.ok == "Quit Anyway"
          and asked_again.cancel == "Keep Waiting"
          and "rendering audio" in asked_again.message,
          f"ok={getattr(asked_again, 'ok', None)!r}")
    check("Keep Waiting keeps the app up, exactly as the first refusal did",
          len(_QUIT_CALLS) == quits_before and app.engine.stream is not None
          and app.midi.port_name is not None)
    app._busy = ""

    # ...and the same dance on a separate app, driven all the way through: a
    # wedged export must not be able to trap the user in a running process.
    _Window.answer = 1                      # "Quit Anyway"
    wedged = JustPianoApp()
    wedged._busy = "rendering audio"
    quits_before = len(_QUIT_CALLS)
    wedged.menu["Quit"].click()             # refused, with the alert
    wedged.menu["Quit"].click()             # confirmed
    check("Quit Anyway is a real way out of a wedged export",
          len(_QUIT_CALLS) == quits_before + 1 and wedged.engine.stream is None
          and wedged.midi.port_name is None and not wedged.timer.started
          and not wedged.panel_timer.started,
          f"quits={len(_QUIT_CALLS) - quits_before} "
          f"stream={wedged.engine.stream!r}")
    del wedged

    # Park the watcher inside its poll so the shutdown really has to join it:
    # otherwise close() races the watcher, which re-opens the port behind it.
    # The park is released from inside shutdown's own `join()` call, never after
    # a fixed timeout: with `gate.wait(0.4)` the window could expire before the
    # Quit click landed (two check()s with print()s sit in between) and a
    # shutdown that joined nothing still passed 80/80 on a loaded machine.
    gate = threading.Event()
    parked = threading.Event()
    joined = threading.Event()
    order: list[str] = []
    real_list_ports = app.midi.list_ports
    real_close = app.midi.close

    def watched_close():
        order.append("close")
        return real_close()

    app.midi.close = watched_close

    def parked_list_ports():
        if not parked.is_set() and threading.current_thread().name == "midi-watch":
            parked.set()
            gate.wait(10.0)
        return real_list_ports()

    app.midi.list_ports = parked_list_ports
    check("the MIDI watcher is parked mid-poll for the shutdown race",
          wait_until(parked.is_set, 10.0, 0.02))
    watcher = app.midi._watcher
    real_join = watcher.join

    def watched_join(timeout=None):
        joined.set()
        order.append("join")
        gate.set()          # let the poll finish only once the join is waiting
        return real_join(timeout)

    watcher.join = watched_join
    check("the parked watcher is still inside its poll when Quit is clicked",
          watcher is not None and watcher.is_alive() and not gate.is_set(),
          f"alive={watcher is not None and watcher.is_alive()} gate={gate.is_set()}")
    app._open_panel()
    check("the keyboard is on screen when Quit is finally clicked",
          panel.is_open and app.panel_timer.started)
    app.menu["Quit"].click()
    check("quitting takes the keyboard down with it",
          not panel.is_open and not app.panel_timer.started,
          f"open={panel.is_open} timer={app.panel_timer.started}")
    check("shutdown joined the watcher instead of racing it", joined.is_set())
    # Ordering matters as much as the join itself: closing first lets a watcher
    # already inside open() reinstall a live port behind the teardown.
    check("shutdown joined the watcher before closing the port",
          "join" in order and "close" in order
          and order.index("join") < order.index("close"),
          str(order))
    check("quit closes the audio stream", app.engine.stream is None)
    check("quit stopped the MIDI watcher",
          watcher is not None and not watcher.is_alive())
    check("quit closes MIDI and nothing re-opens it",
          app.midi.port_name is None
          and not wait_until(lambda: app.midi.port_name is not None, 1.0, 0.05),
          str(app.midi.port_name))
    # shutdown() is terminal: a watcher that arrives late (or a menu click that
    # slipped through) must not be able to reinstall a live port or callback.
    check("MIDI is terminal after shutdown: nothing can re-open a port",
          app.midi.open(None) is False
          and app.midi.open("Twin Keys", index=0) is False
          and app.midi.port_name is None,
          str(app.midi.port_name))
    check("and no new virtual port can be published either",
          app.midi.open_virtual("Just Piano Again") is False)

    # Real PortAudio raises on stop() after close(), and so does the stub, so only
    # the streams the app still owns get stopped here.
    for stream in _STREAMS:
        if not stream.closed:
            stream.stop()

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
