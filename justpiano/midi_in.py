"""
MIDI input handling built on python-rtmidi.

Features:
  * enumerate / open / close hardware MIDI inputs
  * a background watcher that auto-connects when you plug a keyboard in and
    reconnects if it is unplugged -- but only while no device has been chosen by
    hand; once `preferred` is set the watcher attaches to that device only and
    stays disconnected while it is absent
  * an always-available virtual port ("Just Piano") so other apps and DAWs can
    play the engine too

`error` carries the last reason a connection attempt failed (a port held by
another app, typically) for the UI to show; it is cleared as soon as a port
opens or the requested device simply is not plugged in any more.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Container, Optional

# Ports we never auto-select: they are loopbacks or our own virtual port, and
# silently latching onto one would stop us picking up a real keyboard later.
# They are still listed in the menu so they can be chosen by hand.
_AUTO_SKIP = ("iac driver", "justpiano", "network session")

#: How long the auto-pick keeps stepping over a port that refused to open. Long
#: enough that a keyboard genuinely held by another app is asked once every half
#: minute rather than on every 1.5 s watcher tick, short enough that quitting
#: that app gets the keyboard back on its own.
_REFUSAL_TTL = 30.0


class MidiInput:
    def __init__(self, on_message: Callable[[int, int, int, float], None],
                 on_ports_changed: Optional[Callable[[list], None]] = None) -> None:
        self._on_message = on_message
        self._on_ports_changed = on_ports_changed
        # Re-entrant: `open()` holds it for its whole body and calls `close()`.
        self._lock = threading.RLock()
        self._midi_in = None
        self._virtual = None
        self.port_name: Optional[str] = None
        self.preferred: Optional[str] = None
        self._known_ports: list[str] = []
        # Ports whose open() was just refused (device held by another client),
        # each mapped to when it happened: the auto-pick steps over them so it
        # can fall through to the next candidate, but only until the refusal
        # expires (`_REFUSAL_TTL`), the port list changes, or the user asks for
        # a rescan -- a busy keyboard must not be latched out for the session.
        self._open_failed: dict[str, float] = {}
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._closed = False
        self.available = False
        self.error: Optional[str] = None
        self.last_activity = 0.0

        try:
            import rtmidi  # noqa: F401
            self.available = True
        except Exception as exc:
            self.error = f"python-rtmidi not available: {exc}"

    # -------------------------------------------------------------- discovery
    def list_ports(self) -> list[str]:
        if not self.available:
            return []
        try:
            import rtmidi
            probe = rtmidi.MidiIn()
            try:
                return list(probe.get_ports())
            finally:
                probe.delete()
        except Exception:
            return []

    @staticmethod
    def _auto_pick(ports: list[str],
                   skip: Optional[Container[str]] = None) -> Optional[str]:
        """First real hardware port, or None if only loopbacks are present.

        `skip` drops ports we have recently failed to open (a set, or the
        refusal-time dict), so auto mode falls through to the next candidate
        instead of hammering the same one.
        """
        skip = skip if skip is not None else ()
        for name in ports:
            low = name.lower()
            if any(s in low for s in _AUTO_SKIP) or name in skip:
                continue
            return name
        return None

    def _refusals(self) -> dict[str, float]:
        """The refusals still in force, forgetting the ones that have expired.

        A refusal that never expires latches a keyboard out for the rest of the
        session: the app that held it quits, the port list is unchanged, so
        nothing ever cleared the entry and the auto-pick kept stepping over the
        only device the user owns.
        """
        now = time.monotonic()
        for name, refused_at in list(self._open_failed.items()):
            if now - refused_at >= _REFUSAL_TTL:
                del self._open_failed[name]
        return self._open_failed

    # ------------------------------------------------------------- connection
    def open(self, name: Optional[str], index: Optional[int] = None) -> bool:
        """Open a MIDI port.

        `index` addresses a port unambiguously (two identical keyboards share a
        name); `name` is used when restoring a remembered device. Pass both as
        None to auto-select.

        A request for a device that is not present fails instead of substituting
        another keyboard: auto-picking here would contradict the watcher's
        preferred-only policy and show up as a connect-then-drop flap one tick
        later. A failure also never costs the caller a working connection -- the
        replacement port is opened before the old one is released.

        The whole body runs under `_lock`, so a concurrent `open()`/`close()`
        (the watcher thread and the menu both call in) can never leave two live
        rtmidi instances behind, doubling every note.
        """
        if not self.available:
            return False
        import rtmidi

        with self._lock:
            if self._closed:
                # shutdown() is terminal: its watcher join is bounded, so a
                # watcher stalled in list_ports() can still arrive here long
                # after teardown and must not reinstall a live port + callback.
                return False
            ports = self.list_ports()
            target = None
            if index is not None and 0 <= index < len(ports):
                target = ports[index]
            elif name:
                for port in ports:
                    if port == name:
                        target = port
                        break
                if target is None:
                    for port in ports:
                        if name.lower() in port.lower():
                            target = port
                            break
            if target is None:
                if name or index is not None:
                    # A specific device was asked for and it is not there: keep
                    # whatever is playing and let the watcher attach the
                    # remembered keyboard as soon as it reappears.
                    return False
                # Only auto-pick when nothing specific was requested. An
                # explicit auto request -- launch, "Connect Automatically",
                # "Rescan Devices" -- is the user asking us to try everything
                # again, so every refusal is forgiven here. The watcher always
                # asks for a port by name, so a busy one is still not hammered.
                self._open_failed.clear()
                target = self._auto_pick(ports)
                if target is None:
                    self.close()
                    return False
            if index is None and target == self.port_name and self._midi_in is not None:
                return True

            midi_in = None
            try:
                # Re-read the list immediately before opening: rtmidi addresses
                # ports by index, and a device unplugged in between would shift
                # every index after it.
                fresh = self.list_ports()
                if index is not None and 0 <= index < len(fresh) and fresh[index] == target:
                    port_index = index
                elif target in fresh:
                    port_index = fresh.index(target)
                else:
                    return False
                midi_in = rtmidi.MidiIn()
                midi_in.open_port(port_index)
                midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
                midi_in.set_callback(self._callback)
                # Surrender the old port only now that the replacement is live:
                # closing first meant a refused open() (a port held by another
                # client) dropped the keyboard that was working.
                self.close()
                self._midi_in = midi_in
                midi_in = None      # ownership handed over
                self.port_name = target
                self.error = None
                self._open_failed.pop(target, None)
                return True
            except Exception as exc:
                self.error = f'Could not open "{target}": {exc}'
                # Note when the refusal happened so the auto-pick moves on to
                # the next candidate instead of re-requesting this port every
                # 1.5 s -- and so it can try again once the note goes stale.
                self._open_failed[target] = time.monotonic()
                return False
            finally:
                # A half-opened instance would otherwise leak a port on every
                # 1.5 s watcher retry.
                if midi_in is not None:
                    try:
                        midi_in.close_port()
                        midi_in.delete()
                    except Exception:
                        pass

    def close(self) -> None:
        with self._lock:
            if self._midi_in is not None:
                try:
                    self._midi_in.cancel_callback()
                    self._midi_in.close_port()
                    self._midi_in.delete()
                except Exception:
                    pass
                # We had a working port, so any earlier open() failure is stale.
                self.error = None
            self._midi_in = None
            self.port_name = None

    def open_virtual(self, name: str = "Just Piano") -> bool:
        if not self.available or self._closed or self._virtual is not None:
            return self._virtual is not None
        try:
            import rtmidi
            virtual = rtmidi.MidiIn()
            virtual.open_virtual_port(name)
            virtual.ignore_types(sysex=True, timing=True, active_sense=True)
            virtual.set_callback(self._callback)
            self._virtual = virtual
            return True
        except Exception:
            self._virtual = None
            return False

    # ---------------------------------------------------------------- watcher
    def start_watcher(self, interval: float = 1.5) -> None:
        # shutdown() is terminal for the port and the virtual port, so it has to
        # be terminal for the polling too: a watcher started after teardown
        # would sit there listing ports for a dead app.
        if self._closed:
            return
        if self._watcher and self._watcher.is_alive():
            return
        self._stop.clear()
        self._watcher = threading.Thread(target=self._watch, args=(interval,),
                                         name="midi-watch", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()

    def _watch(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                ports = self.list_ports()
                if ports != self._known_ports:
                    self._known_ports = ports
                    # The devices changed, so give ports that refused to open
                    # earlier another chance.
                    self._open_failed.clear()
                    if self._on_ports_changed:
                        try:
                            self._on_ports_changed(ports)
                        except Exception:
                            pass
                # (Re)connect when our port vanished or nothing is connected.
                if self.port_name is not None and self.port_name not in ports:
                    self.close()
                if self.preferred:
                    # Only ever attach to the remembered device, and migrate
                    # back to it as soon as it reappears -- never let open()'s
                    # auto-pick substitute a different keyboard.
                    if self.preferred in ports:
                        if self.port_name != self.preferred:
                            self.open(self.preferred)
                    else:
                        # Absent, not refused: drop a stale open() diagnostic so
                        # the UI does not blame the app for an unplugged cable.
                        self.error = None
                        if self.port_name is not None:
                            self.close()
                else:
                    # Auto mode: always migrate to real hardware when it shows
                    # up, even if we are currently sitting on a loopback port.
                    # `_refusals()` expires stale ones, so a keyboard released
                    # by another app is picked up without the port list having
                    # to change and without the menu being touched.
                    best = self._auto_pick(ports, self._refusals())
                    if best is not None and best != self.port_name:
                        self.open(best)
            except Exception:
                pass

    # --------------------------------------------------------------- dispatch
    def _callback(self, event, data=None) -> None:
        message, _delta = event
        if not message:
            return
        status = message[0]
        if status < 0x80 or status >= 0xF0:
            # Below 0x80 is a data byte, not a status byte; 0xF0 and up is
            # system/realtime traffic we do not play.
            return
        # Channel-voice messages are three bytes, except program change and
        # channel pressure, which are two. Anything shorter is a truncated
        # message: a bare [0x90] used to be played and recorded as note 0 at
        # velocity 0.
        if len(message) < (2 if (status & 0xF0) in (0xC0, 0xD0) else 3):
            return
        d1 = message[1]
        d2 = message[2] if len(message) > 2 else 0
        # One read of the clock into a local: with two callback threads sharing
        # `last_activity`, reading it back could hand on the *other* thread's
        # timestamp, which is one more way for events to arrive out of order.
        when = time.monotonic()
        self.last_activity = when
        try:
            self._on_message(status, d1, d2, when)
        except Exception:
            pass

    def shutdown(self) -> None:
        # Record the terminal state first: the join below is bounded, so a
        # watcher stalled inside list_ports() can still reach open() after
        # teardown -- the flag (checked under `_lock`) is what stops it
        # reinstalling a live port and callback behind us.
        self._closed = True
        self.stop_watcher()
        # Join before closing, otherwise a watcher already inside open() would
        # install a fresh port after teardown and keep feeding a stopped engine.
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(timeout=2.0)
        self.close()
        if self._virtual is not None:
            try:
                self._virtual.cancel_callback()
                self._virtual.close_port()
                self._virtual.delete()
            except Exception:
                pass
            self._virtual = None
