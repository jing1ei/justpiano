"""
The AppKit half of the on-screen keyboard: the view that paints it, the popover
it lives in, and the status-item click that opens it.

Nothing here is imported at module load. `pyobjc` is only touched from inside
`_build_classes()`, so this module imports fine on a machine without AppKit -
`available()` then says so, `create_panel()` returns None, and the tray falls
back to dropping the menu on a click exactly as it did before. That is the same
"degrade, never raise" contract `macui` follows.

Why a popover. `NSPopover` in `NSPopoverBehaviorTransient` mode already does
three things we would otherwise have to build by hand on a borderless
`NSPanel`: it anchors itself to the status item button (and slides along the
screen edge so a 900 pt wide keyboard next to the right-hand corner stays fully
visible), it dismisses itself as soon as the user clicks anywhere else, and it
follows the system appearance. A panel would need a window level, an
activation policy, a global event monitor for the click-away and manual
positioning arithmetic - more code, in the layer we cannot test here.

That self-dismissal happens on the *mouse-down* that lands outside the popover,
while the status item delivers its action on mouse-*up* (see `CLICK_MASK`), so
by the time a click on the menu bar icon is reported the popover is already
gone. `KeyboardPanel.dismissed_at` is how the tray tells "this click is what
closed it" from "the keyboard was not on screen": see the popover delegate in
`_build_classes()`. Keeping the behaviour Transient is deliberate - AppKit
still owns the dismissal, so no failure in here can leave the keyboard stuck on
screen.

Why the status item has to be re-wired at all: see `intercept_status_item()`.
"""

from __future__ import annotations

import time
import traceback
import types
import weakref
from typing import Callable, Optional

from . import keyboard

PANEL_MARGIN = 8.0
HEADER_HEIGHT = 22.0
GEAR_SIZE = 18.0
LABEL_FONT_SIZE = 8.5

#: The mute button sits next to the gear, same size, with a gap wide enough that
#: neither is hit by accident.
MUTE_SIZE = GEAR_SIZE
BUTTON_GAP = 7.0

#: SF Symbols (macOS 11+) for the mute button, with text glyphs for anything
#: older - the same fallback the gear uses.
MUTE_SYMBOLS = {True: "speaker.slash.fill", False: "speaker.wave.2.fill"}
MUTE_GLYPHS = {True: "\U0001F507", False: "\U0001F509"}

#: NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp. The masks are 1 << the
#: NSEventType (leftMouseUp = 2, rightMouseUp = 4), which is what lets a single
#: target/action see both buttons.
CLICK_MASK = (1 << 2) | (1 << 4)

#: How long a queued `popUpStatusItemMenu:` is assumed to still be coming. A
#: zero-delay `performSelector:` fires on the very next pass of the run loop, so
#: anything older than this never fired at all (a run loop that never came back)
#: and must not be allowed to make the menu unreachable.
POP_UP_COALESCE = 0.5

_CLASSES = None
_TRIED = False

#: Inside `popUpStatusItemMenu:`'s tracking loop, and "a pop-up is queued for the
#: next run loop pass, at this time". Main thread only, like everything here.
_POPPING = False
_POP_UP_QUEUED_AT: Optional[float] = None


def available() -> bool:
    """Whether the AppKit half can run at all (i.e. pyobjc is importable)."""
    return _build_classes() is not None


# --------------------------------------------------------------- ObjC classes
def _color(ak, rgb):
    return ak.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)


def _build_classes():
    """Define the Objective-C subclasses once, lazily, on the main thread.

    Returns a namespace of what the panel needs, or None without pyobjc. The
    result (including the failure) is cached: an ObjC class can only be defined
    once per name in a process.
    """
    global _CLASSES, _TRIED
    if _TRIED:
        return _CLASSES
    _TRIED = True
    try:
        import AppKit as ak
        from Foundation import NSAttributedString, NSMakePoint, NSMakeRect, NSMakeSize, NSObject
    except Exception:
        return None

    font_key = getattr(ak, "NSFontAttributeName", "NSFont")
    color_key = getattr(ak, "NSForegroundColorAttributeName", "NSColor")

    def draw_label(op) -> None:
        attrs = {font_key: ak.NSFont.systemFontOfSize_(LABEL_FONT_SIZE),
                 color_key: _color(ak, op.fill)}
        text = NSAttributedString.alloc().initWithString_attributes_(op.text, attrs)
        size = text.size()
        # The view is flipped, so "op.y + op.height" is the front edge of the
        # key: sit the octave name just above it.
        text.drawAtPoint_(NSMakePoint(op.x + (op.width - size.width) / 2.0,
                                      op.y + op.height - size.height - 3.0))

    class JustPianoKeyboardView(ak.NSView):
        """Paints `keyboard.draw_plan()` and turns clicks into notes.

        The Python-side `controller` attribute (a `keyboard.KeyboardController`)
        is assigned right after construction; every entry point tolerates its
        absence so a half-built view can never crash AppKit.
        """

        def isFlipped(self):
            # keyboard.py measures y from the far end of the keys towards the
            # player, which is exactly a flipped view.
            return True

        def acceptsFirstMouse_(self, event):
            # A popover is not the active window: without this, the first click
            # inside it would only activate the app instead of playing a note.
            return True

        def drawRect_(self, dirty_rect):
            try:
                controller = getattr(self, "controller", None)
                if controller is None:
                    return
                _color(ak, keyboard.BACKGROUND).setFill()
                ak.NSBezierPath.fillRect_(self.bounds())
                for op in controller.draw_plan():
                    if op.kind != "key":
                        draw_label(op)
                        continue
                    path = ak.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        NSMakeRect(op.x, op.y, op.width, op.height), 2.0, 2.0)
                    _color(ak, op.fill).setFill()
                    path.fill()
                    if op.stroke is not None:
                        _color(ak, op.stroke).setStroke()
                        path.setLineWidth_(0.75)
                        path.stroke()
            except Exception:
                traceback.print_exc()

        def mouseDown_(self, event):
            try:
                controller = getattr(self, "controller", None)
                if controller is None:
                    return
                point = self.convertPoint_fromView_(event.locationInWindow(), None)
                controller.mouse_down(point.x, point.y)
                self.setNeedsDisplay_(True)
            except Exception:
                traceback.print_exc()

        def mouseDragged_(self, event):
            try:
                controller = getattr(self, "controller", None)
                if controller is None:
                    return
                point = self.convertPoint_fromView_(event.locationInWindow(), None)
                controller.mouse_dragged(point.x, point.y)
                self.setNeedsDisplay_(True)
            except Exception:
                traceback.print_exc()

        def mouseUp_(self, event):
            try:
                controller = getattr(self, "controller", None)
                if controller is None:
                    return
                controller.mouse_up()
                self.setNeedsDisplay_(True)
            except Exception:
                traceback.print_exc()

    class JustPianoActionTarget(NSObject):
        """Target for the status item button and the panel's two buttons.

        AppKit holds a target *weakly*, so whoever installs one of these has to
        keep it referenced - the tray keeps the status item's, the panel keeps
        its own. Every selector reads a plain Python attribute (`on_click`,
        `on_settings`, `on_mute`) that the installer sets, and reads it with
        `getattr`: a handler that was never assigned is a no-op rather than an
        `AttributeError` on its way into ObjC. (The defaults are done that way
        rather than in an `init` override - PyObjC turns every method defined in
        the class body into a selector, so an initialiser here would have to hand
        `self` back through `objc.super`, which is a sharper edge than a
        `getattr` for the same result.)
        """

        def statusItemClicked_(self, sender):
            try:
                handler = getattr(self, "on_click", None)
                if handler is not None:
                    handler(_is_secondary_click(ak))
            except Exception:
                traceback.print_exc()

        def settingsClicked_(self, sender):
            try:
                handler = getattr(self, "on_settings", None)
                if handler is not None:
                    handler()
            except Exception:
                traceback.print_exc()

        def muteClicked_(self, sender):
            try:
                handler = getattr(self, "on_mute", None)
                if handler is not None:
                    handler()
            except Exception:
                traceback.print_exc()

    class JustPianoPopoverDelegate(NSObject):
        """The keyboard popover's delegate, for one message: `popoverDidClose:`.

        `NSPopover` sends it to its delegate after the popover has closed - the
        only notification of *when* the transient behaviour dismissed itself,
        which is what lets the tray know the click it is about to be told about
        is the one that closed the keyboard.

        `NSPopover.delegate` is a weak property, so `KeyboardPanel` keeps this
        object referenced for as long as the popover exists.
        """

        def popoverDidClose_(self, notification):
            try:
                handler = getattr(self, "on_close", None)
                if handler is not None:
                    handler()
            except Exception:
                traceback.print_exc()

    class JustPianoMenuPopper(NSObject):
        """Pops a status item menu on the next pass of the run loop.

        `performSelector:withObject:afterDelay:` retains its receiver until the
        selector has been performed, so an instance of this can be built,
        scheduled and dropped on the floor. See `pop_up_menu()` for why the
        pop-up must not happen inside the caller's own event handling.
        """

        def popUpNow_(self, _sender):
            try:
                handler = getattr(self, "on_fire", None)
                if handler is not None:
                    handler()
            except Exception:
                traceback.print_exc()

    _CLASSES = types.SimpleNamespace(
        ak=ak,
        KeyboardView=JustPianoKeyboardView,
        ActionTarget=JustPianoActionTarget,
        PopoverDelegate=JustPianoPopoverDelegate,
        MenuPopper=JustPianoMenuPopper,
        rect=NSMakeRect,
        size=NSMakeSize,
        # NSRectEdgeMinY: hang the popover below the menu bar.
        min_y_edge=getattr(ak, "NSRectEdgeMinY", getattr(ak, "NSMinYEdge", 1)),
    )
    return _CLASSES


def _is_secondary_click(ak) -> bool:
    """True when the click that triggered the action was a right/control click."""
    try:
        event = ak.NSApplication.sharedApplication().currentEvent()
        if event is None:
            return False
        secondary = (getattr(ak, "NSEventTypeRightMouseUp", 4),
                     getattr(ak, "NSEventTypeRightMouseDown", 3))
        if int(event.type()) in secondary:
            return True
        control = getattr(ak, "NSEventModifierFlagControl", 1 << 18)
        return bool(int(event.modifierFlags()) & control)
    except Exception:
        return False


def _activate(ak) -> None:
    """Bring this accessory app forward so the popover takes mouse clicks."""
    try:
        app = ak.NSApplication.sharedApplication()
        if hasattr(app, "activate"):            # macOS 14+
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _click_landed_on(ak, view) -> bool:
    """Is the event AppKit is handling right now a mouse-down on `view`?

    Asked from `popoverDidClose:`, where the current event is the mouse-down
    that made a transient popover dismiss itself, to tell "the user clicked the
    menu bar icon" (the click is about to arrive as an action, and must leave the
    keyboard closed) from "the user clicked something else" (a later click on the
    icon is a plain request to open it).

    Anything unknown - no current event, no window yet, an event this process
    never saw because the click was in another application - answers True: the
    cost of a false yes is one swallowed click inside the grace window the tray
    applies, the cost of a false no is the panel reopening itself.
    """
    try:
        event = ak.NSApplication.sharedApplication().currentEvent()
        if event is None or view is None:
            return True
        kind = int(event.type())
        keys = (getattr(ak, "NSEventTypeKeyDown", 10),
                getattr(ak, "NSEventTypeKeyUp", 11),
                getattr(ak, "NSEventTypeFlagsChanged", 12))
        if kind in keys:
            # Escape, or typing into whatever is focused: a known non-click, so
            # nothing is about to arrive as an action on mouse-up.
            return False
        down = (getattr(ak, "NSEventTypeLeftMouseDown", 1),
                getattr(ak, "NSEventTypeRightMouseDown", 3))
        if kind not in down:
            return True
        window = view.window()
        if window is None:
            return True
        # The status item's button owns its own status bar window, so that
        # window's frame *is* the icon's rectangle on screen - which is the
        # coordinate space NSEvent.mouseLocation reports in.
        point = ak.NSEvent.mouseLocation()
        frame = window.frame()
        return bool(frame.origin.x <= point.x <= frame.origin.x + frame.size.width
                    and frame.origin.y <= point.y <= frame.origin.y + frame.size.height)
    except Exception:
        return True


# ---------------------------------------------------------------- the panel
class KeyboardPanel:
    """The 88-key keyboard in a transient popover under the menu bar icon.

    The tray only ever uses five members, and the smoke test's stand-in mirrors
    exactly those: :attr:`is_open`, :meth:`open`, :meth:`close`, :meth:`redraw`
    and :meth:`refresh_mute`. :attr:`dismissed_at` and :meth:`forget_dismissal`
    are the sixth and seventh, and the tray reads them defensively (`getattr`):
    a panel that cannot report a dismissal is only as good as this code was
    before the delegate existed, never worse.
    """

    def __init__(self, controller, status_item, classes) -> None:
        self._controller = controller
        self._status_item = status_item
        self._c = c = classes
        ak = c.ak

        board = controller.keyboard
        width = board.width + 2 * PANEL_MARGIN
        height = board.height + HEADER_HEIGHT + 2 * PANEL_MARGIN

        content = ak.NSView.alloc().initWithFrame_(c.rect(0.0, 0.0, width, height))
        view = c.KeyboardView.alloc().initWithFrame_(
            c.rect(PANEL_MARGIN, PANEL_MARGIN, board.width, board.height))
        view.controller = controller
        content.addSubview_(view)

        target = c.ActionTarget.alloc().init()
        target.on_settings = controller.settings_clicked
        target.on_mute = controller.mute_clicked
        target.on_click = lambda _secondary: None

        gear = ak.NSButton.alloc().initWithFrame_(
            c.rect(width - PANEL_MARGIN - GEAR_SIZE,
                   height - PANEL_MARGIN - GEAR_SIZE, GEAR_SIZE, GEAR_SIZE))
        gear.setBordered_(False)
        gear.setToolTip_("Settings")
        image = None
        try:                                    # SF Symbols: macOS 11+
            image = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "gearshape.fill", "Settings")
        except Exception:
            image = None
        if image is not None:
            gear.setImage_(image)
            gear.setTitle_("")
        else:
            gear.setTitle_("\u2699")            # gear glyph
        gear.setTarget_(target)
        gear.setAction_("settingsClicked:")
        content.addSubview_(gear)

        # One click to silence the piano, right where the keys are. Left of the
        # gear, so the gear does not move for anyone used to it.
        mute = ak.NSButton.alloc().initWithFrame_(
            c.rect(width - PANEL_MARGIN - GEAR_SIZE - BUTTON_GAP - MUTE_SIZE,
                   height - PANEL_MARGIN - MUTE_SIZE, MUTE_SIZE, MUTE_SIZE))
        mute.setBordered_(False)
        mute.setTarget_(target)
        mute.setAction_("muteClicked:")
        content.addSubview_(mute)

        try:
            label = ak.NSTextField.labelWithString_("Just Piano")
            label.setFrame_(c.rect(PANEL_MARGIN, height - PANEL_MARGIN - GEAR_SIZE,
                                   200.0, GEAR_SIZE))
            label.setTextColor_(ak.NSColor.secondaryLabelColor())
            content.addSubview_(label)
        except Exception:
            pass

        view_controller = ak.NSViewController.alloc().init()
        view_controller.setView_(content)

        popover = ak.NSPopover.alloc().init()
        popover.setContentViewController_(view_controller)
        popover.setContentSize_(c.size(width, height))
        popover.setBehavior_(getattr(ak, "NSPopoverBehaviorTransient", 1))
        popover.setAnimates_(False)

        self._view = view
        self._popover = popover
        self._view_controller = view_controller
        self._mute_button = mute
        self._gear_target = target              # AppKit only holds it weakly

        #: When the popover last went away *on its own*, from `time.monotonic()`,
        #: or None when the last close was one this code asked for. Written from
        #: `popoverDidClose:` (main thread), read by the tray.
        self._dismissed_at: Optional[float] = None
        self._closing = False                   # a close() of ours is in flight
        self._delegate = self._install_delegate()

        self.refresh_mute()                     # a panel built while muted

    def _install_delegate(self):
        """Wire `popoverDidClose:` so a self-dismissal can be timestamped.

        Optional, like everything else here: if the delegate cannot be built the
        popover still opens, closes and dismisses itself - the tray simply loses
        the "this click closed it" hint and reopens on such a click, which is what
        it did before.
        """
        try:
            delegate = self._c.PopoverDelegate.alloc().init()
        except Exception:
            traceback.print_exc()
            return None
        # A weak reference, so the delegate's Python attribute cannot keep the
        # panel (and with it the popover) alive through a reference cycle.
        panel_ref = weakref.ref(self)

        def closed(_ref=panel_ref) -> None:
            panel = _ref()
            if panel is not None:
                panel._note_closed()

        delegate.on_close = closed
        try:
            # NSPopover.delegate is a weak property: the returned object is kept
            # in self._delegate for exactly that reason.
            self._popover.setDelegate_(delegate)
        except Exception:
            traceback.print_exc()
            return None
        return delegate

    def _note_closed(self) -> None:
        """`popoverDidClose:` arrived (main thread).

        A close this code asked for says nothing about the user's intent, so it
        clears the stamp instead of setting one - otherwise putting the keyboard
        away for the menu would swallow the next click on the icon. A dismissal
        is only stamped when the mouse-down that caused it was on the status item
        button, i.e. when the click that is about to be delivered on mouse-up is
        that same click.
        """
        if self._closing:
            self._closing = False
            self._dismissed_at = None
            return
        button = None
        try:
            button = self._status_item.button()
        except Exception:
            button = None
        if _click_landed_on(self._c.ak, button):
            self._dismissed_at = time.monotonic()

    @property
    def dismissed_at(self) -> Optional[float]:
        """`time.monotonic()` of the last dismissal the *user* caused, or None."""
        return self._dismissed_at

    def forget_dismissal(self) -> None:
        """Drop the stamp once the tray has acted on it, so the click after that
        one opens the keyboard again instead of being swallowed too."""
        self._dismissed_at = None

    @property
    def is_open(self) -> bool:
        try:
            return bool(self._popover.isShown())
        except Exception:
            return False

    def open(self) -> bool:
        """Show the keyboard under the menu bar icon. True once it is up."""
        try:
            button = self._status_item.button()
            if button is None:
                return False
            _activate(self._c.ak)
            self._closing = False
            self._dismissed_at = None       # whatever closed it last is history
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, self._c.min_y_edge)
        except Exception:
            traceback.print_exc()
            return False
        return self.is_open

    def close(self) -> None:
        try:
            # Only a close of something that is actually on screen will produce a
            # `popoverDidClose:`; flagging one that cannot arrive would make the
            # *next* real dismissal look like ours.
            self._closing = bool(self.is_open)
            self._popover.close()
        except Exception:
            traceback.print_exc()

    def redraw(self) -> None:
        """Repaint the keys (main thread only - it is AppKit)."""
        try:
            self._view.setNeedsDisplay_(True)
        except Exception:
            traceback.print_exc()

    def refresh_mute(self) -> None:
        """Show the controller's mute state on the mute button.

        Called by the tray after *any* of the three toggles, so the button, the
        menu checkmark and the menu bar icon are always the same fact drawn three
        ways. Main thread only.
        """
        ak = self._c.ak
        muted = bool(getattr(self._controller, "muted", False))
        try:
            image = None
            try:                                # SF Symbols: macOS 11+
                image = ak.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    MUTE_SYMBOLS[muted], "Unmute" if muted else "Mute")
            except Exception:
                image = None
            if image is not None:
                self._mute_button.setImage_(image)
                self._mute_button.setTitle_("")
            else:
                self._mute_button.setImage_(None)
                self._mute_button.setTitle_(MUTE_GLYPHS[muted])
            self._mute_button.setToolTip_("Unmute" if muted else "Mute")
        except Exception:
            traceback.print_exc()


def create_panel(controller, status_item) -> Optional[KeyboardPanel]:
    """Build the keyboard popover, or None when it cannot exist here."""
    classes = _build_classes()
    if classes is None or status_item is None or controller is None:
        return None
    try:
        return KeyboardPanel(controller, status_item, classes)
    except Exception:
        traceback.print_exc()
        return None


# ------------------------------------------------------- status item plumbing
def _make_click_handler(on_click: Callable[[bool], None]):
    """An ObjC target whose `statusItemClicked:` calls `on_click(secondary)`."""
    classes = _build_classes()
    if classes is None:
        return None
    handler = classes.ActionTarget.alloc().init()
    handler.on_click = on_click
    handler.on_settings = lambda: None
    handler.on_mute = lambda: None
    return handler


def intercept_status_item(status_item, on_click: Callable[[bool], None]):
    """Take the click on the menu bar icon away from rumps' menu.

    rumps assigns the main `NSMenu` to the status item (`NSApp
    .initializeStatusBar`, rumps.py:954), and AppKit then pops that menu itself
    on mouse-down before any action can fire. Clearing it and giving the status
    item's *button* a target/action is the only way to see the click.

    Nothing about the menu itself changes: the `NSMenu`, its items, their rumps
    targets (`NSApp.callback_`) and their key equivalents are all untouched, and
    `pop_up_menu()` presents that same object on demand - from the gear button,
    or from a right click here. So `_internal.call_as_function_or_method`
    dispatch, `rumps.Timer` and the Quit item all keep working.

    Returns the handler, which the caller must keep referenced (AppKit holds a
    target weakly), or None when pyobjc is unavailable - in which case the
    status item is left exactly as rumps set it up.
    """
    handler = _make_click_handler(on_click)
    if handler is None or status_item is None:
        return None
    try:
        button = status_item.button()
        if button is None:
            return None
        status_item.setMenu_(None)
        button.setTarget_(handler)
        button.setAction_("statusItemClicked:")
        try:
            # Without this the button only reports the left button, and a right
            # click would do nothing at all now that the menu is gone.
            button.sendActionOn_(CLICK_MASK)
        except Exception:
            traceback.print_exc()
        return handler
    except Exception:
        traceback.print_exc()
        return None


def pop_up_menu(status_item, nsmenu) -> bool:
    """Drop `nsmenu` from the status item, as an un-intercepted click would.

    Scheduled with `performSelector:withObject:afterDelay:0` rather than run
    here: the callers are the gear button's action (delivered from inside
    `NSButton`'s mouse-tracking loop, with the popover around it tearing down)
    and the status item's own action, and `popUpStatusItemMenu:` runs a modal
    tracking loop of its own. `performSelector:...afterDelay:` queues the work in
    the *default* run loop mode, so it cannot run inside a tracking loop at all:
    it happens once the current event has been dealt with, which is the documented
    idiom for "do this, but not from inside this event". Without pyobjc (or if
    scheduling fails) it happens immediately, which is what the headless suites
    see - the menu must never be one failed schedule away from unreachable.
    """
    if status_item is None or nsmenu is None:
        return False
    if _schedule_pop_up(status_item, nsmenu):
        return True
    return _pop_up_now(status_item, nsmenu)


def _schedule_pop_up(status_item, nsmenu) -> bool:
    """Queue `_pop_up_now` for the next run loop pass. False = not scheduled."""
    global _POP_UP_QUEUED_AT
    classes = _build_classes()
    if classes is None:
        return False
    now = time.monotonic()
    if _POP_UP_QUEUED_AT is not None and now - _POP_UP_QUEUED_AT < POP_UP_COALESCE:
        # One queued pop-up is enough: a second would open the menu again after
        # the first one's tracking loop ended, i.e. after the user was done. An
        # older stamp than that means the run loop never came back for it, and
        # coalescing onto it forever would make the menu unreachable.
        return True
    try:
        popper = classes.MenuPopper.alloc().init()

        def fire() -> None:
            global _POP_UP_QUEUED_AT
            _POP_UP_QUEUED_AT = None
            _pop_up_now(status_item, nsmenu)

        popper.on_fire = fire
        # performSelector:withObject:afterDelay: retains the receiver until the
        # selector has been performed, so `popper` needs no owner here.
        popper.performSelector_withObject_afterDelay_("popUpNow:", None, 0.0)
    except Exception:
        traceback.print_exc()
        return False
    _POP_UP_QUEUED_AT = now
    return True


def _pop_up_now(status_item, nsmenu) -> bool:
    """Show the menu, right now.

    `popUpStatusItemMenu:` is soft-deprecated but remains the only API that
    positions and highlights a status item menu properly. When it throws, the
    fallback is to hand the click back to rumps' own arrangement - assign the
    menu and stop there, so the *next* mouse-down on the icon opens it, which is
    what AppKit does with a status item that has a menu. (`performClick_` was
    never an alternative: a status item pops its menu on mouse-down, before any
    action is sent, and the action it *does* send would land on the click handler
    this module installed and come straight back here.) The keyboard is lost in
    that case; the menu, with Quit in it, is not.
    """
    global _POPPING
    if status_item is None or nsmenu is None:
        return False
    if _POPPING:
        # Re-entered from inside `popUpStatusItemMenu:`'s own tracking loop.
        return False
    _POPPING = True
    try:
        try:
            status_item.popUpStatusItemMenu_(nsmenu)
            return True
        except Exception:
            traceback.print_exc()
        try:
            status_item.setMenu_(nsmenu)
            return True
        except Exception:
            traceback.print_exc()
            return False
    finally:
        _POPPING = False
