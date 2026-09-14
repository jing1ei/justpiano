"""
Just Piano - macOS menu bar app.

Plug in a MIDI keyboard, play. Hit record, play, export a .mid (or .wav).
Everything lives in the menu bar; there is no main window.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import rumps

from . import hotkey, keyboardview, macui, recorder as rec, tone
from .config import RECORDINGS_DIR, Settings
from .keyboard import KeyboardController
from .midi_in import MidiInput
from .recorder import CONTROL_CHANGE, NOTE_OFF, NOTE_ON
from . import sampled
from .sampled import make_bank
from .synth import AudioEngine, list_output_devices

ICON_IDLE = "🎹"
ICON_REC = "⏺"
ICON_NOTE = "🎵"
#: Menu bar fallback while muted: only used when the muted image cannot be
#: loaded (a source checkout that has never run `tools/make_icon.py`). An app
#: that has gone silent with nothing to show for it is the one outcome this
#: feature must never produce.
ICON_MUTED = "🔇"

#: The muted menu bar image, rendered by `tools/make_icon.py` as a template
#: image so macOS recolours it for the light and dark menu bar.
MUTED_ICON_FILE = "icon-muted.png"

#: Redraw rate of the on-screen keyboard. Only ever runs while the panel is
#: visible; `_tick`'s 0.5 s is for the menu, and far too slow to look played.
PANEL_REFRESH = 1.0 / 60.0

#: How long after the keyboard popover dismissed itself a click on the menu bar
#: icon still counts as the click that dismissed it (see `_panel_was_open`). Long
#: enough to cover the mouse-down-to-mouse-up of any ordinary click, short enough
#: that "click away, then click the icon" - two separate clicks, with the trip to
#: the menu bar in between - is never mistaken for one.
PANEL_DISMISS_GRACE = 0.3

VOLUME_STEPS = [("25%", 0.25), ("50%", 0.5), ("75%", 0.75), ("100%", 1.0), ("125%", 1.25)]
REVERB_CHOICES = [("Off", "off"), ("Room", "room"), ("Concert Hall", "hall")]
TOUCH_CHOICES = [("Light", "soft"), ("Normal", "normal"), ("Heavy", "hard")]
#: How loud the recorded action is. Only the sampled instruments have any -- the
#: modelled ones carry their mechanical noise inside the note itself -- so this
#: submenu is disabled while one of those is playing rather than lying about
#: having an effect.
KEY_NOISE_CHOICES = [("Off", 0.0), ("Subtle", 0.12), ("Natural", 0.25),
                     ("Prominent", 0.55)]
LATENCY_CHOICES = [("Lowest (~3 ms)", 128), ("Balanced (~6 ms)", 256), ("Safest (~12 ms)", 512)]

#: The "let macOS choose" row of the Audio Output submenu, and the one title in
#: it that does *not* name a device. It is therefore reserved: an interface that
#: is itself called "System Default" (a virtual device can be named anything)
#: would otherwise be a second row with the same title - unreachable through
#: `output_menu[title]`, and read by `_pick_output` as "no device at all". It is
#: listed as "System Default (2)" instead and is selectable like any other.
DEFAULT_OUTPUT_LABEL = "System Default"

#: Title of the instrument submenu, shared with the smoke test so the two
#: cannot drift apart.
VOICING_MENU_TITLE = "Instrument"

#: The MIDI submenu's two command rows, reserved against port names for exactly
#: the same reason.
MIDI_AUTO_LABEL = "Connect Automatically"
MIDI_RESCAN_LABEL = "Rescan Devices"

#: Mute hotkey presets, in menu order; None is "Off". Deliberately a short list
#: rather than a shortcut recorder.
#:
#: The default is ⌃⌥⌘M: a global monitor cannot swallow the key, so whatever is
#: chosen also reaches the DAW that is playing the piano. Logic, Ableton and FL
#: all bind plain M and most two-modifier combinations, but three modifiers plus
#: a letter is not a shape any of them ships (and M for mute is where a user will
#: look). ⌃⌥M is the lighter alternative for a keyboard where ⌘ is awkward, and
#: F13 is for anyone with a full-size keyboard and nothing bound to it.
HOTKEY_SPECS = [None, "ctrl+alt+cmd+m", "ctrl+alt+m", "f13"]
HOTKEY_OFF_LABEL = "Off"

#: How many times `_tick` may ask `AXIsProcessTrusted()` while a hotkey is armed
#: and its global monitor is missing. At the timer's 0.5 s that is about two
#: minutes of watching for the permission to appear - long enough to cover the
#: walk to System Settings, and *bounded*, which polling a permission the user has
#: decided against was not. The submenu's "Allow Accessibility Access…" line stays
#: clickable, and clicking it starts a fresh window (`_open_accessibility`), so a
#: grant that arrives later is never unreachable.
TRUST_POLL_LIMIT = 240

#: The Accessibility line in the hotkey submenu. Only the "denied" and "unknown"
#: states are clickable - the others have nothing the user could act on.
ACCESS_TITLE_OFF = "Global Access: Not Needed"
ACCESS_TITLE_OK = "Global Access: Granted"
ACCESS_TITLE_DENIED = "Allow Accessibility Access…"
ACCESS_TITLE_UNKNOWN = "Open Accessibility Settings…"
ACCESS_TITLE_UNAVAILABLE = "Global Access: Unavailable"


def asset_path(name: str) -> str | None:
    """Absolute path of a bundled image, or None when it is not there.

    `rumps.App.icon` builds the NSImage eagerly and raises for a missing file
    (rumps.py:110), so every caller has to look first: a checkout that has never
    run `tools/make_icon.py` must lose the icon, not the app.

    Inside a PyInstaller bundle the assets are the spec's `datas`. PyInstaller 6
    unpacks those into `JustPiano.app/Contents/Resources` and cross-links them
    into `Contents/Frameworks`, which is where `sys._MEIPASS` points - so the
    first root is normally the whole answer, and the second is the same files
    reached without depending on that link. Running from source it is the
    `assets/` directory next to the `justpiano` package.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(os.path.join(meipass, "assets"))
        roots.append(os.path.join(os.path.dirname(meipass), "Resources", "assets"))
    roots.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "assets"))
    for root in roots:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


def unique_labels(names, reserved=()):
    """Suffix repeated titles so every entry stays reachable.

    `rumps.Menu.add` keys an item by its title, but it hands `__setitem__` the
    `_choose_key` sentinel (rumps.py:262-266), which is never a key already in
    the dict - so `addItem_` runs unconditionally and two identical titles give
    two identical *rows*, with `menu[title]` bound to the second one and the
    first orphaned: not selectable, not updatable, and indistinguishable to the
    user. Two "USB MIDI" keyboards would be one dead row and one live one.

    `reserved` are titles the menu already uses for something else (the "System
    Default" output, say), so a device that happens to be called that gets the
    suffix instead of colliding with it.
    """
    used: set[str] = set(reserved)
    labels = []
    for name in names:
        # A driver may already report "Twin Keys (2)", so keep bumping the
        # counter until the candidate collides with nothing we emitted before.
        label, n = name, 1
        while label in used:
            n += 1
            label = f"{name} ({n})"
        used.add(label)
        labels.append(label)
    return labels


class JustPianoApp(rumps.App):
    def __init__(self) -> None:
        # template=True: the muted image is a black-on-alpha template, which is
        # what lets macOS invert it for a dark menu bar and for a highlighted
        # status item, exactly as it does the emoji title it replaces.
        super().__init__("Just Piano", title=ICON_IDLE, quit_button=None,
                         template=True)

        self.settings = Settings()
        self.bank = make_bank(self.settings["voicing"])
        self.recorder = rec.Recorder()
        self.engine = AudioEngine(
            self.bank,
            blocksize=int(self.settings["blocksize"]),
            volume=float(self.settings["volume"]),
            reverb_preset=self.settings["reverb"],
            velocity_curve=self.settings["velocity_curve"],
            strike_variation=float(self.settings["strike_variation"]),
            key_noise=float(self.settings["key_noise"]),
            resonance=float(self.settings["resonance"]),
        )
        self.midi = MidiInput(self._on_midi, self._on_ports_changed)
        self.midi.preferred = self.settings["midi_port"]

        self._ports: list[str] = []
        self._port_labels: dict[str, tuple[int, str]] = {}
        self._output_labels: dict[str, int] = {}
        self._voicing_labels: dict[str, str] = {}   # menu label -> voicing id
        self._hotkey_labels: dict[str, str | None] = {}   # menu label -> spec
        self._midi_sig = None
        self._busy = ""            # non-empty while exporting
        #: Has a Quit already been refused for *this* render? The second ask is
        #: offered as "Quit Anyway" (see `_confirm_quit_while_busy`), so a wedged
        #: worker can never make the app unquittable.
        self._quit_refused_while_busy = False
        self._export_result = None  # (title, message, subtitle, path) from the worker
        self._last_note_at = 0.0
        self._notes_played = 0
        self._notes_lock = threading.Lock()   # two MIDI callback threads bump it
        self._audio_started = False

        # Mute. The state itself lives in the engine (the audio thread needs it
        # anyway); these two are only about how it is *shown*.
        self._muted_icon = asset_path(MUTED_ICON_FILE)
        self._icon_shown = False   # is the muted image actually up?

        # The mute hotkey, armed from rumps' before_start once there is a run
        # loop to dispatch the monitors.
        self.hotkeys = hotkey.HotkeyMonitor(self._hotkey_pressed)
        self._hotkey_rearm = False
        self._trust_sig: bool | None = None
        #: How many times `_tick` has asked Accessibility since the hotkey was
        #: armed. Bounded (see `_tick`): a permission that is simply not going to
        #: be granted must not cost an API call twice a second for the session.
        self._trust_polls = 0

        # The on-screen keyboard. Everything here is pure Python; AppKit is only
        # touched once _install_panel() builds the popover on the main thread.
        self.keys = KeyboardController(on_note_on=self._mouse_note_on,
                                       on_note_off=self._mouse_note_off,
                                       on_settings=self._show_menu,
                                       on_mute=self._toggle_mute)
        self.panel = None
        self._click_handler = None    # AppKit holds a button's target weakly

        self._build_menu()
        # rumps builds the NSStatusItem inside run(), and emits before_start
        # immediately afterwards (rumps.py:1191) - the first moment there is a
        # status item to take the click away from.
        rumps.events.before_start.register(self._install_panel)
        rumps.events.before_start.register(self._install_hotkey)
        self._boot()

    # --------------------------------------------------------------- menu tree
    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("Starting…")
        self.status_item.set_callback(None)

        self.midi_menu = rumps.MenuItem("MIDI Keyboard")
        self.sound_menu = rumps.MenuItem("Sound")
        self.output_menu = rumps.MenuItem("Audio Output")

        self.volume_menu = rumps.MenuItem("Volume")
        for label, value in VOLUME_STEPS:
            self.volume_menu.add(rumps.MenuItem(label, callback=self._pick_volume))
        # "Instrument", not "Piano Voicing": two of the five are electric pianos,
        # which are not a voicing of anything. The persisted key stays `voicing`
        # -- renaming it would strand every existing settings.json and every
        # cache file named after one.
        self.voicing_menu = rumps.MenuItem(VOICING_MENU_TITLE)
        # Two voicings sharing a label would give the submenu two identical rows,
        # only the second of which `voicing_menu[label]` can reach (see
        # `unique_labels`), leaving the other unselectable: run the labels through
        # unique_labels() and remember which id each one means.
        for label, voicing in zip(
                unique_labels([tone.VOICING_LABELS.get(v, v) for v in tone.VOICINGS]),
                tone.VOICINGS):
            self._voicing_labels[label] = voicing
            self.voicing_menu.add(rumps.MenuItem(label, callback=self._pick_voicing))
        self.reverb_menu = rumps.MenuItem("Reverb")
        for label, _v in REVERB_CHOICES:
            self.reverb_menu.add(rumps.MenuItem(label, callback=self._pick_reverb))
        self.key_noise_menu = rumps.MenuItem("Key Noise")
        for label, _v in KEY_NOISE_CHOICES:
            self.key_noise_menu.add(rumps.MenuItem(label, callback=self._pick_key_noise))
        self.touch_menu = rumps.MenuItem("Touch Response")
        for label, _v in TOUCH_CHOICES:
            self.touch_menu.add(rumps.MenuItem(label, callback=self._pick_touch))
        self.latency_menu = rumps.MenuItem("Latency")
        for label, _v in LATENCY_CHOICES:
            self.latency_menu.add(rumps.MenuItem(label, callback=self._pick_latency))

        self.sound_menu.add(self.voicing_menu)
        self.sound_menu.add(self.volume_menu)
        self.sound_menu.add(self.reverb_menu)
        self.sound_menu.add(self.touch_menu)
        self.sound_menu.add(self.key_noise_menu)
        self.sound_menu.add(rumps.separator)
        self.sound_menu.add(self.latency_menu)
        self.sound_menu.add(self.output_menu)

        self.record_item = rumps.MenuItem("Start Recording", callback=self._toggle_record,
                                          key="r")
        self.take_item = rumps.MenuItem("No recording yet")
        self.take_item.set_callback(None)
        self.save_midi_item = rumps.MenuItem("Export MIDI File…", callback=self._save_midi,
                                             key="e")
        self.save_wav_item = rumps.MenuItem("Export Audio (WAV)…", callback=self._save_wav)
        self.discard_item = rumps.MenuItem("Discard Recording", callback=self._discard)
        self.save_session_item = rumps.MenuItem("Export Everything Since Launch…",
                                                callback=self._save_session)

        self.login_item = rumps.MenuItem("Open at Login", callback=self._toggle_login)

        # Mute: one click, top level, right next to the sound settings it
        # belongs with. Deliberately *without* a key equivalent: a status menu's
        # shortcut only works while that menu is open, and printing "⌘M" beside
        # the item would advertise a second, weaker shortcut next to the real
        # global one below.
        self.mute_item = rumps.MenuItem("Mute", callback=self._toggle_mute)
        self.hotkey_menu = rumps.MenuItem("Mute Hotkey")
        for spec in HOTKEY_SPECS:
            label = HOTKEY_OFF_LABEL if spec is None else hotkey.label(spec)
            if label is None:                  # a preset that does not parse
                continue
            self._hotkey_labels[label] = spec
            self.hotkey_menu.add(rumps.MenuItem(label, callback=self._pick_hotkey))
        self.hotkey_menu.add(rumps.separator)
        self.access_item = rumps.MenuItem(ACCESS_TITLE_OFF)
        self.access_item.set_callback(None)
        self.hotkey_menu.add(self.access_item)

        self.menu = [
            self.status_item,
            rumps.separator,
            self.midi_menu,
            self.sound_menu,
            rumps.separator,
            self.mute_item,
            self.hotkey_menu,
            rumps.separator,
            self.record_item,
            self.take_item,
            self.save_midi_item,
            self.save_wav_item,
            self.discard_item,
            rumps.separator,
            self.save_session_item,
            rumps.MenuItem("Open Recordings Folder", callback=self._open_folder),
            rumps.separator,
            rumps.MenuItem("Panic (All Notes Off)", callback=self._panic),
            self.login_item,
            rumps.MenuItem("About Just Piano", callback=self._about),
            rumps.MenuItem("Quit", callback=self._quit, key="q"),
        ]

        self._sync_sound_checkmarks()
        self._refresh_outputs()
        self._refresh_login_state()
        # No mute change to show yet, so the launch title stays the idle glyph
        # rumps was constructed with (see `_refresh_mute_state`).
        self._refresh_mute_state(retitle=False)
        self._refresh_hotkey_state()

        self.timer = rumps.Timer(self._tick, 0.5)
        self.timer.start()
        # Started and stopped with the panel, so a closed keyboard costs nothing.
        self.panel_timer = rumps.Timer(self._panel_tick, PANEL_REFRESH)

    # ------------------------------------------------------------------- boot
    def _boot(self) -> None:
        # No completion callback: `_tick` already reads `bank.progress`,
        # `bank.ready` and `bank.error` twice a second, which is what draws the
        # status line - a second notification path would have nothing to say.
        self.bank.load_or_build_async()
        self.midi.open_virtual("Just Piano")
        self.midi.open(self.settings["midi_port"])
        self.midi.start_watcher()
        self._ports = self.midi.list_ports()
        self._rebuild_midi_menu()
        self._start_audio()

    def _output_device_labels(self, devices):
        """Menu labels for `devices`, in order. One definition, because
        `_start_audio` has to reproduce exactly what `_refresh_outputs` listed -
        including the "System Default" row the labels must not collide with."""
        return unique_labels([name for _index, name in devices],
                             reserved=(DEFAULT_OUTPUT_LABEL,))

    def _start_audio(self) -> None:
        device = self.settings["output_device"]
        index = None
        if device:
            index = self._output_labels.get(device)
            if index is None:
                devices = list_output_devices()
                for (i, _n), label in zip(devices,
                                          self._output_device_labels(devices)):
                    if label == device:
                        index = i
                        break
        self._audio_started = self.engine.start(index)
        if not self._audio_started and index is not None:
            self._audio_started = self.engine.start(None)  # fall back to default

    # ------------------------------------------------------------- MIDI input
    def _on_midi(self, status: int, d1: int, d2: int, when: float) -> None:
        """Called on an rtmidi callback thread - keep it fast, never touch the UI.
        Both the hardware port and the virtual "Just Piano" port have their own
        callback thread, so two of them can be in here at the same time.

        The on-screen keyboard is fed from here too, and only through
        `NoteLights`: one O(1) dict write under a lock held for a couple of
        instructions. The redraw itself belongs to the main thread's panel timer,
        which is the only one allowed to talk to AppKit.
        """
        kind = status & 0xF0
        if kind == NOTE_ON and d2 > 0:
            self.engine.note_on(d1, d2)
            self.keys.lights.press(d1, d2)
            self._last_note_at = when
            with self._notes_lock:   # += is a read-modify-write; don't lose notes
                self._notes_played += 1
        elif kind == NOTE_OFF or (kind == NOTE_ON and d2 == 0):
            self.engine.note_off(d1)
            self.keys.lights.release(d1)
        elif kind == CONTROL_CHANGE:
            self.engine.control_change(d1, d2)
            if d1 in (120, 123):     # All Sound Off / All Notes Off: keys come up
                self.keys.lights.release_all()
        # Deliberately *without* `when`: the recorder stamps the event inside its
        # own lock, so the two rtmidi callback threads cannot append in one order
        # while carrying timestamps taken in the other. Passing the clock read
        # from up here is what inverted 66 525 events in a session and made
        # `span()` (and the exported duration) go negative. `_last_note_at` above
        # is a UI read-out, not a timeline, so it keeps using `when`.
        self.recorder.handle(status, d1, d2)

    def _on_ports_changed(self, ports: list[str]) -> None:
        self._ports = ports  # menu is rebuilt from the timer (main thread)

    @staticmethod
    def _clear_submenu(menu) -> None:
        # rumps only creates the backing NSMenu when the first child is added,
        # so calling clear() on an empty submenu raises AttributeError.
        if len(menu):
            menu.clear()

    def _rebuild_midi_menu(self) -> None:
        self._clear_submenu(self.midi_menu)
        if not self.midi.available:
            item = rumps.MenuItem("python-rtmidi not installed")
            item.set_callback(None)
            self.midi_menu.add(item)
            return
        if not self._ports:
            item = rumps.MenuItem("No MIDI device found")
            item.set_callback(None)
            self.midi_menu.add(item)
        self._port_labels = {}
        # The two command rows below share the submenu with the ports, and a
        # driver can name a port anything: reserve their titles for the same
        # reason DEFAULT_OUTPUT_LABEL is reserved.
        for index, (label, name) in enumerate(
                zip(unique_labels(self._ports,
                                  reserved=(MIDI_AUTO_LABEL, MIDI_RESCAN_LABEL)),
                    self._ports)):
            self._port_labels[label] = (index, name)
            item = rumps.MenuItem(label, callback=self._pick_midi_port)
            item.state = 1 if name == self.midi.port_name else 0
            self.midi_menu.add(item)
        self.midi_menu.add(rumps.separator)
        auto = rumps.MenuItem(MIDI_AUTO_LABEL, callback=self._pick_midi_auto)
        auto.state = 1 if not self.settings["midi_port"] else 0
        self.midi_menu.add(auto)
        self.midi_menu.add(rumps.MenuItem(MIDI_RESCAN_LABEL, callback=self._rescan_midi))

    def _pick_midi_port(self, sender) -> None:
        index, name = self._port_labels.get(sender.title, (None, sender.title))
        self.settings["midi_port"] = name
        self.midi.preferred = name
        self.midi.open(name, index=index)
        self._midi_sig = None          # force a redraw of the checkmarks
        self._rebuild_midi_menu()

    def _pick_midi_auto(self, _sender) -> None:
        self.settings["midi_port"] = None
        self.midi.preferred = None
        self.midi.open(None)
        self._rebuild_midi_menu()

    def _rescan_midi(self, _sender) -> None:
        self._ports = self.midi.list_ports()
        if self.midi.port_name is None:
            self.midi.open(self.settings["midi_port"])
        self._rebuild_midi_menu()

    # ------------------------------------------------------------------ sound
    def _sync_sound_checkmarks(self) -> None:
        vol = float(self.settings["volume"])
        for label, value in VOLUME_STEPS:
            self.volume_menu[label].state = 1 if abs(value - vol) < 1e-6 else 0
        for label, value in self._voicing_labels.items():
            self.voicing_menu[label].state = (
                1 if value == self.settings["voicing"] else 0)
        for label, value in REVERB_CHOICES:
            self.reverb_menu[label].state = 1 if value == self.settings["reverb"] else 0
        has_action = getattr(self.bank, "noise_scale", None) is not None
        for label, value in KEY_NOISE_CHOICES:
            item = self.key_noise_menu[label]
            item.state = 1 if value == self.settings["key_noise"] else 0
            item.set_callback(self._pick_key_noise if has_action else None)
        for label, value in TOUCH_CHOICES:
            self.touch_menu[label].state = 1 if value == self.settings["velocity_curve"] else 0
        for label, value in LATENCY_CHOICES:
            self.latency_menu[label].state = 1 if value == int(self.settings["blocksize"]) else 0

    def _pick_volume(self, sender) -> None:
        value = dict(VOLUME_STEPS)[sender.title]
        self.settings["volume"] = value
        self.engine.set_volume(value)
        self._sync_sound_checkmarks()

    def _pick_voicing(self, sender) -> None:
        voicing = self._voicing_labels.get(sender.title)
        if voicing is None:
            return
        if voicing == self.bank.voicing and not self.bank.error:
            # Already playing it: re-picking must not cost a rebuild (a
            # first-time voicing is a 1-2 s render). A voicing whose build
            # *failed* is the exception -- re-picking it is the retry.
            self._sync_sound_checkmarks()
            return
        if self._busy:
            # The WAV renderer reads self.bank on its own thread; same answer
            # the export path gives when it is asked to do two things at once.
            macui.alert("Please wait", f"Still {self._busy}…")
            return
        self.settings["voicing"] = voicing
        self._swap_bank(voicing)
        self._sync_sound_checkmarks()

    def _swap_bank(self, voicing: str) -> None:
        """Point the engine at another voicing's sample bank.

        Ordering is the whole point, and it is `AudioEngine.restart()`'s with the
        swap slipped into the middle: the audio callback mixes numpy views into
        the bank's buffers, so it is stopped (PortAudio joins it) and every voice
        dropped *before* `bank`/`engine.bank` point anywhere else. Nothing can
        then be halfway through a block, holding a buffer of the bank being
        replaced, or leaving a note stuck on a pedal latch.

        The new bank is installed empty and rendered in the background exactly as
        at launch: `note_on` drops keys that are not rendered yet, `_tick` already
        draws `bank.progress`/`bank.error`, and this returns immediately so the
        menu never blocks on a 1-2 s build.
        """
        self.engine.stop()                          # no callback runs past here
        self.engine.all_notes_off(immediate=True)   # no voice holds an old buffer
        self.engine.reverb.reset()                  # nor a tail of the old tone
        bank = make_bank(voicing)
        self.bank = bank
        self.engine.bank = bank
        bank.load_or_build_async()
        self._start_audio()

    def _pick_reverb(self, sender) -> None:
        value = dict(REVERB_CHOICES)[sender.title]
        self.settings["reverb"] = value
        self.engine.set_reverb(value)
        self._sync_sound_checkmarks()

    def _pick_key_noise(self, sender) -> None:
        value = dict(KEY_NOISE_CHOICES)[sender.title]
        self.settings["key_noise"] = value
        self.engine.set_key_noise(value)
        self._sync_sound_checkmarks()

    def _pick_touch(self, sender) -> None:
        value = dict(TOUCH_CHOICES)[sender.title]
        self.settings["velocity_curve"] = value
        self.engine.set_velocity_curve(value)
        self._sync_sound_checkmarks()

    def _pick_latency(self, sender) -> None:
        value = dict(LATENCY_CHOICES)[sender.title]
        self.settings["blocksize"] = value
        self.engine.blocksize = value
        self._audio_started = self.engine.restart()
        self._sync_sound_checkmarks()

    def _refresh_outputs(self) -> None:
        self._clear_submenu(self.output_menu)
        current = self.settings["output_device"]
        default = rumps.MenuItem(DEFAULT_OUTPUT_LABEL, callback=self._pick_output)
        default.state = 1 if not current else 0
        self.output_menu.add(default)
        self.output_menu.add(rumps.separator)
        devices = list_output_devices()
        self._output_labels = {}
        for (index, _name), label in zip(devices,
                                         self._output_device_labels(devices)):
            self._output_labels[label] = index
            item = rumps.MenuItem(label, callback=self._pick_output)
            item.state = 1 if label == current else 0
            self.output_menu.add(item)

    def _pick_output(self, sender) -> None:
        # `_output_labels` is the list of real devices, and no device can be
        # labelled DEFAULT_OUTPUT_LABEL (`_output_device_labels` reserves it), so
        # that title unambiguously means "let macOS choose".
        label = sender.title
        self.settings["output_device"] = (
            None if label not in self._output_labels else label)
        self._start_audio()
        self._refresh_outputs()

    # ------------------------------------------------------------------- mute
    def _toggle_mute(self, _sender=None) -> None:
        """The menu item, the panel's button and the hotkey all arrive here."""
        self.set_muted(not self.engine.muted)

    def set_muted(self, muted: bool) -> None:
        """The single place mute changes, whichever entry point asked.

        Main thread only (menu callbacks, the panel button and both NSEvent
        monitors all run on the run loop). The engine gets the request first, and
        every piece of UI is then redrawn from `engine.muted`, so the checkmark,
        the panel button and the menu bar icon cannot drift apart or fight each
        other - and the menu bar title is refreshed here rather than waiting up
        to half a second for `_tick`.

        Deliberately *not* persisted. A DAW session is the transient thing here,
        and an app that launches silent is indistinguishable from an app that is
        broken - the support cost of that beats one click after a relaunch. The
        hotkey choice, which is a preference rather than a state, is persisted.
        """
        self.engine.set_muted(bool(muted))
        self._refresh_mute_state()

    def _refresh_mute_state(self, *, retitle: bool = True) -> None:
        """Redraw every place the mute state is shown, from `engine.muted`.

        `retitle=False` is for the one call that happens while the app is still
        being built (`_build_menu`): the launch title belongs to
        `rumps.App.__init__` - the idle glyph - and to `_tick` from then on. A
        mute refresh with no mute change to show must not turn the first thing
        the user ever sees into a "0%" read-out for a sample build `_boot` has
        not even started yet.

        The order in which the title and the image are swapped is *visible*:
        rumps' `NSApp.fallbackOnName` (rumps.py:964) puts the application name in
        the menu bar the moment the status item has neither a title nor an image,
        and both `App.title` and `App.icon` run through it. So whichever of the
        two is about to carry the menu bar is installed first and the other is
        dropped afterwards. The other way round, every toggle flashes
        "Just Piano" at the user.
        """
        muted = self.engine.muted
        self.mute_item.state = 1 if muted else 0
        self.mute_item.title = "Muted" if muted else "Mute"
        if muted:
            self._apply_icon(True)              # image first, then drop the glyph
            if retitle:
                self.title = self._menu_bar_title()
        else:
            if retitle:
                self.title = self._menu_bar_title()   # glyph back before the image goes
            self._apply_icon(False)
        if self.keys.set_muted(muted) and self.panel is not None:
            self.panel.refresh_mute()

    def _apply_icon(self, muted: bool) -> None:
        """Swap the menu bar image for the mute state (AppKit, main thread).

        Unmuted there is no image at all - the emoji title is the icon, as it
        always was - so this only ever installs or removes the muted one. rumps
        loads the file here and raises if it has gone missing, which must cost
        the image and nothing else: `_menu_bar_title` then falls back to a glyph.
        """
        path = self._muted_icon if muted else None
        if path is None and self.icon is None:
            self._icon_shown = False
            return                  # nothing installed, nothing to remove
        try:
            self.icon = path
        except Exception:
            path = None
            try:
                self.icon = None
            except Exception:
                pass
        self._icon_shown = path is not None

    def _menu_bar_title(self) -> str:
        """The menu bar text for the current state.

        Split out of `_tick` so the headless suites can assert on it. While the
        muted image is up, that image is the piano: the idle glyph would only
        repeat it, so it is dropped, and what a picture cannot say (the record
        timer, the sample build percentage) is kept. Without the image, 🔇 says
        it in text instead.
        """
        if self.recorder.recording:
            glyph, detail = ICON_REC, self._fmt_time(self.recorder.elapsed)
        elif not self.bank.ready:
            glyph, detail = ICON_IDLE, f"{int(self.bank.progress * 100)}%"
        elif time.monotonic() - self._last_note_at < 0.35:
            glyph, detail = ICON_NOTE, ""
        else:
            glyph, detail = ICON_IDLE, ""
        if self.engine.muted:
            idle = glyph in (ICON_IDLE, ICON_NOTE)
            if self._icon_shown:
                glyph = "" if idle else glyph
            else:
                glyph = ICON_MUTED if idle else f"{ICON_MUTED} {glyph}"
        return " ".join(part for part in (glyph, detail) if part)

    # ----------------------------------------------------------------- hotkey
    def _install_hotkey(self) -> None:
        """Arm the persisted hotkey (main thread, from rumps' before_start).

        Not done in `__init__`: the monitors belong to the run loop that rumps
        starts immediately after this event, which is also the thread their
        handlers are dispatched on.
        """
        self._apply_hotkey(self.settings["mute_hotkey"])

    def _apply_hotkey(self, spec, *, rearm: bool = True) -> None:
        """Arm `spec` (None = off) and bring the submenu in line.

        `rearm` is the one retry Accessibility deserves: a user can arm a hotkey
        before granting the permission, and `_tick` re-applies it once when trust
        appears. `rearm=False` is that re-application, and it *clears* the flag
        whatever the outcome - a global monitor that AppKit still refuses (the
        token is documented as nullable) must not queue another attempt, or a
        permission problem that survives the retry becomes an install twice a
        second forever, each one taking the working local monitor down and up
        again in between.
        """
        self.hotkeys.apply(spec)
        self._hotkey_rearm = (rearm and self.hotkeys.hotkey is not None
                              and not self.hotkeys.global_installed)
        # A fresh arm is also a fresh reason to watch for trust appearing.
        self._trust_polls = 0
        self._refresh_hotkey_state()

    def _pick_hotkey(self, sender) -> None:
        if sender.title not in self._hotkey_labels:
            return
        spec = self._hotkey_labels[sender.title]
        self.settings["mute_hotkey"] = spec
        self._apply_hotkey(spec)

    def _hotkey_pressed(self) -> None:
        """The hotkey fired. Runs on the main thread: NSEvent monitor handlers
        are dispatched by the run loop, so touching the menu here is safe."""
        self._toggle_mute(None)

    def _refresh_hotkey_state(self) -> None:
        # Compare *parsed* specs, not the strings: settings.json is a file a user
        # can edit, and "cmd+ctrl+alt+m" is the same shortcut as the preset's
        # "ctrl+alt+cmd+m" - it arms correctly, so it has to be the one ticked.
        # `parse` is total (nonsense is None), and None is the Off entry, which is
        # also the truth for a spec that did not parse: nothing is armed.
        current = hotkey.parse(self.settings["mute_hotkey"])
        armed = self.hotkeys.hotkey
        for label, spec in self._hotkey_labels.items():
            self.hotkey_menu[label].state = 1 if hotkey.parse(spec) == current else 0
        # Only ask macOS while the answer can change something. "Unknown" (the
        # symbol could not be bound) must never be reported as "denied", but it
        # still offers the pane - it is the only thing the user could act on.
        # Without event monitors at all (no pyobjc: running from source on a
        # machine that never installed it) there is nothing Accessibility could
        # fix, and pointing at that pane would be a wild goose chase.
        trusted = hotkey.trusted() if armed is not None else None
        self._trust_sig = trusted
        if armed is None:
            title, callback = ACCESS_TITLE_OFF, None
        elif not hotkey.available():
            title, callback = ACCESS_TITLE_UNAVAILABLE, None
        elif trusted is True:
            title, callback = ACCESS_TITLE_OK, None
        elif trusted is False:
            title, callback = ACCESS_TITLE_DENIED, self._open_accessibility
        else:
            title, callback = ACCESS_TITLE_UNKNOWN, self._open_accessibility
        self.access_item.title = title
        self.access_item.set_callback(callback)

    def _open_accessibility(self, _sender) -> None:
        """Open the pane the global monitor needs. Never prompts by itself: the
        prompting API would nag on every launch, a click never does.

        Also restarts `_tick`'s bounded watch for the permission (see
        TRUST_POLL_LIMIT): the user is on their way to grant it right now, which
        is the one moment worth watching for again.
        """
        self._trust_polls = 0
        macui.open_url(hotkey.ACCESSIBILITY_PANE)

    # -------------------------------------------------------------- recording
    def _toggle_record(self, _sender) -> None:
        if self.recorder.recording:
            self.recorder.stop()
        else:
            if self.recorder.take and not self._confirm_overwrite():
                return
            self.recorder.start()
        self._refresh_take_state()

    def _confirm_overwrite(self) -> bool:
        # Nothing tracks whether the take was exported, so the dialog must not
        # claim that it was not: say what actually happens instead.
        window = rumps.Window(
            title="Start a new recording?",
            message=("Starting a new recording replaces the current take. "
                     "Export it first if you want to keep it."),
            ok="Replace", cancel="Cancel", dimensions=(0, 0),
        )
        return bool(window.run().clicked)

    def _discard(self, _sender) -> None:
        self.recorder.discard()
        self._refresh_take_state()

    def _refresh_take_state(self) -> None:
        # Runs twice a second: read the recorder's own counters (one locked O(1)
        # snapshot) instead of rescanning the live take/session buffers while the
        # MIDI threads append to them.
        take_notes, take_seconds, session_notes = self.recorder.stats()
        if self.recorder.recording:
            self.record_item.title = "Stop Recording"
            self.take_item.title = f"Recording… {self._fmt_time(self.recorder.elapsed)}"
        else:
            self.record_item.title = "Start Recording"
            if take_notes:
                self.take_item.title = (
                    f"Take: {self._fmt_time(take_seconds)} · {take_notes} notes")
            elif self.recorder.take:
                # Pedal/CC/pitch-bend only: still a take, just a silent one.
                self.take_item.title = f"Take: {self._fmt_time(take_seconds)} · no notes"
            else:
                self.take_item.title = "No recording yet"

        # One definition of "there is a take", shared with the overwrite prompt
        # in _toggle_record: a take holding only controller events must stay
        # exportable and discardable instead of being silently unreachable.
        has_take = bool(self.recorder.take) and not self.recorder.recording
        self.save_midi_item.set_callback(self._save_midi if has_take else None)
        self.save_wav_item.set_callback(self._save_wav if has_take else None)
        self.discard_item.set_callback(self._discard if has_take else None)

        self.save_session_item.title = (
            f"Export Everything Since Launch… ({session_notes} notes)"
            if session_notes else "Export Everything Since Launch…")
        self.save_session_item.set_callback(self._save_session if session_notes else None)

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = int(seconds)
        return f"{seconds // 60}:{seconds % 60:02d}"

    # ----------------------------------------------------------------- export
    def _save_midi(self, _sender) -> None:
        self._export(self.recorder.snapshot("take"), "mid")

    def _save_wav(self, _sender) -> None:
        self._export(self.recorder.snapshot("take"), "wav")

    def _save_session(self, _sender) -> None:
        self._export(self.recorder.snapshot("session"), "mid",
                     prompt="Export everything played since launch")

    def _export(self, events, ext: str, prompt: str = "Export performance") -> None:
        if not events:
            macui.alert("Nothing to export", "Play a few notes first.")
            return
        if self._busy:
            macui.alert("Please wait", f"Still {self._busy}…")
            return
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        path = macui.save_panel(rec.default_filename(ext=ext), RECORDINGS_DIR, ext, prompt)
        if not path:
            return
        if not path.lower().endswith("." + ext):
            path += "." + ext

        if ext == "mid":
            try:
                rec.export_midi(events, path)
            except Exception as exc:
                macui.alert("Export failed", str(exc))
                return
            macui.notify("MIDI exported", os.path.basename(path),
                         f"{rec.Recorder.note_count(events)} notes")
            macui.reveal_in_finder(path)
            return

        # WAV rendering can take a few seconds -> do it off the main thread.
        # _tick already renders "⏳ Rendering audio…" from self._busy.
        self._busy = "rendering audio"
        # Each render gets its own "are you sure" cycle: a Quit refused for the
        # *last* one must not force-quit this one on the first click.
        self._quit_refused_while_busy = False

        def work():
            # Notifications go through AppKit, which must only be driven from the
            # main thread: leave the outcome behind and let _tick post it.
            try:
                rec.export_wav(events, self.bank, path,
                               volume=float(self.settings["volume"]),
                               reverb=self.settings["reverb"],
                               velocity_curve=self.settings["velocity_curve"])
                self._export_result = ("Audio exported", os.path.basename(path),
                                       f"{rec.Recorder.duration(events):.0f} seconds",
                                       path)
            except Exception as exc:
                self._export_result = ("Export failed", str(exc), "", None)
            finally:
                self._busy = ""

        try:
            threading.Thread(target=work, name="export-wav", daemon=True).start()
        except Exception as exc:
            # The worker never ran, so its `finally` will never clear the flag:
            # a wedged _busy blocks every later export *and* _quit, and Quit is
            # the only way out of an app built with quit_button=None.
            self._busy = ""
            macui.alert("Export failed", str(exc))

    def _open_folder(self, _sender) -> None:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        macui.open_path(RECORDINGS_DIR)

    # -------------------------------------------------- on-screen keyboard
    def _install_panel(self) -> None:
        """Give the menu bar icon a keyboard panel instead of a menu drop.

        Runs on the main thread from rumps' `before_start` event, i.e. after
        `NSApp.initializeStatusBar()` has created the status item and put the
        menu on it. Everything is optional: without pyobjc (or without a status
        item, as in the headless suites) the click keeps dropping the menu, which
        is the behaviour this replaces rather than breaks.
        """
        status_item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if status_item is None:
            return
        self.panel = keyboardview.create_panel(self.keys, status_item)
        if self.panel is None:
            return
        self._click_handler = keyboardview.intercept_status_item(
            status_item, self._status_item_clicked)
        if self._click_handler is None:
            # The menu is still on the status item, so nothing must open the
            # panel: two ways to open at once would fight over the click.
            self.panel = None
            return
        self._refresh_mute_state()   # the button starts out showing the truth

    def _status_item_clicked(self, secondary: bool = False) -> None:
        """The menu bar icon was clicked: left opens the keyboard, right (or
        control-click) drops the menu, the way macOS users expect."""
        if secondary:
            self._show_menu()
        elif self._panel_was_open():
            self._close_panel()
        else:
            self._open_panel()

    def _panel_was_open(self) -> bool:
        """Was the keyboard on screen when this click *started*?

        Not the same question as `panel.is_open`, and that is the whole point.
        The status item's action is delivered on mouse-up (`keyboardview
        .CLICK_MASK`), while `NSPopoverBehaviorTransient` dismisses the popover on
        the mouse-*down* that lands outside its window - so a click on the icon
        that closed the keyboard arrives with `is_open` already False, and reading
        that alone made the click reopen the panel it had just closed (blink shut,
        blink back, 60 Hz timer restarted). Only clicking elsewhere ever closed it.

        The popover's `popoverDidClose:` delegate stamps a self-dismissal it
        believes was caused by a click on the status item button, and a stamp
        younger than PANEL_DISMISS_GRACE means "that was this click": leave the
        keyboard closed and consume the stamp, so the *next* click opens it again.
        A panel that cannot report a dismissal (no delegate, or the smoke test's
        stand-in) reports no stamp and behaves as before, which is why this is all
        `getattr`.
        """
        panel = self.panel
        if panel is None:
            return False
        if panel.is_open:
            return True
        dismissed_at = getattr(panel, "dismissed_at", None)
        if dismissed_at is None:
            return False
        if time.monotonic() - dismissed_at >= PANEL_DISMISS_GRACE:
            return False
        forget = getattr(panel, "forget_dismissal", None)
        if forget is not None:
            forget()
        return True

    def _open_panel(self) -> None:
        if self.panel is None:
            self._show_menu()      # never leave the click doing nothing at all
            return
        self.keys.refresh()        # show keys that are already down
        if not self.panel.open():
            self._show_menu()
            return
        self.panel_timer.start()   # rumps.Timer.start() is a no-op while alive

    def _close_panel(self) -> None:
        self.keys.mouse_up()       # a note the mouse still holds must not stick
        self.panel_timer.stop()
        if self.panel is not None:
            self.panel.close()

    def _show_menu(self) -> None:
        """Open the existing menu - the panel's gear button and the right click
        both come here. The NSMenu is rumps' own object, untouched: every item,
        callback and key equivalent still works, Quit included.

        The keyboard goes away first and the menu is popped afterwards, from the
        next pass of the run loop (`keyboardview.pop_up_menu` schedules it with
        `performSelector:withObject:afterDelay:0`): the gear button calls this
        from inside its own mouse-tracking loop, and running `popUpStatusItemMenu:`
        - a modal tracking loop of its own - on top of that while the popover
        underneath it is being torn down is exactly the nesting that
        `setAnimates_(False)` was papering over.
        """
        if self.panel is not None and self.panel.is_open:
            self._close_panel()
        status_item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        keyboardview.pop_up_menu(status_item, getattr(self.menu, "_menu", None))

    def _panel_tick(self, _timer) -> None:
        """~60 Hz while the keyboard is on screen, and only then.

        Runs on the main thread (rumps.Timer is an NSTimer on the run loop), so
        this is where the MIDI threads' note state becomes pixels.
        """
        if self.panel is None or not self.panel.is_open:
            # Dismissed by clicking away: stop paying for the frames.
            self._close_panel()
            return
        if self.keys.refresh():
            self.panel.redraw()

    def _mouse_note_on(self, note: int, velocity: int) -> None:
        """A key was clicked: play it through the same path as a MIDI note, so it
        lights up, counts and records exactly like a played one."""
        self._on_midi(NOTE_ON, note, velocity, time.monotonic())

    def _mouse_note_off(self, note: int) -> None:
        self._on_midi(NOTE_OFF, note, 0, time.monotonic())

    # ------------------------------------------------------------------ misc
    def _panic(self, _sender) -> None:
        self.engine.all_notes_off(immediate=True)
        self.keys.lights.release_all()
        # Never blank the reverb buffers from here: only the audio thread owns
        # them, so ask the engine to clear them on its next block.
        self.engine.reset_reverb()

    def _refresh_login_state(self) -> None:
        # Deliberately does *not* query System Events at launch: that would pop
        # an Automation permission prompt before the user has done anything.
        if not macui.bundle_path():
            self.login_item.set_callback(None)
            self.login_item.title = "Open at Login (build the .app first)"
            return
        self.login_item.state = 1 if self.settings["login_item"] else 0

    def _toggle_login(self, sender) -> None:
        path = macui.bundle_path()
        if not path:
            return
        want = not bool(sender.state)
        # The user just clicked, so querying System Events (and its Automation
        # prompt) is acceptable here - unlike at launch. The persisted flag goes
        # stale as soon as the entry is removed in System Settings, and acting on
        # it produced a bogus "Could not update login items" alert.
        # `is_login_item` returns None when the query itself failed (Automation
        # denied): only a definite answer may skip the write, otherwise turning
        # the item off would clear the checkmark while the app keeps launching.
        listed = macui.is_login_item(path)
        if listed is not None and listed == want:
            sender.state = 1 if want else 0
            self.settings["login_item"] = want
            return
        if macui.set_login_item(path, want):
            sender.state = 1 if want else 0
            self.settings["login_item"] = want
        else:
            macui.alert("Could not update login items",
                        "Grant Just Piano permission to control System Events in "
                        "System Settings › Privacy & Security › Automation.")

    def _about(self, _sender) -> None:
        # The credit is not a nicety. The recordings are CC-BY 3.0, and the one
        # thing that licence asks in return is that the person who made them is
        # named wherever they are used -- so it is shown whenever a sampled bank
        # is the one sounding, not buried in a file nobody opens.
        sampled_now = getattr(self.bank, "stereo", False)
        layers = len(getattr(self.bank, "layer_velocities", (0, 0)))
        engine = ("88 keys × %d velocity layers, stereo, %d Hz"
                  % (layers, self.engine.samplerate)) if sampled_now else (
                  "88 keys × %d velocity layers, %d Hz"
                  % (layers, self.engine.samplerate))
        macui.alert(
            "Just Piano",
            "Instruments for your MIDI keyboard, in the menu bar.\n\n"
            f"Instrument: {tone.VOICING_LABELS.get(self.bank.voicing, self.bank.voicing)}"
            f"{' (sampled)' if sampled_now else ' (synthesised)'}\n"
            f"Engine: {engine}\n"
            f"Output latency: ~{self.engine.latency_ms:.0f} ms\n"
            f"Recordings: {RECORDINGS_DIR}\n\n"
            "Virtual MIDI port \"Just Piano\" is always available, so other apps "
            "can play this piano too."
            + ("\n\n" + "\n\n".join(sampled.credits()) if sampled_now else ""),
        )

    def _quit(self, _sender) -> None:
        if self._busy and not self._confirm_quit_while_busy():
            return
        try:
            # Nothing may fire into a half-torn-down app: both timers are on the
            # run loop, and rumps swallows what a callback raises (rumps.py:730-735)
            # so a _tick landing on a closed MIDI port or a stopped engine would
            # be invisible rather than harmless.
            self.timer.stop()
            self.panel_timer.stop()
        except Exception:
            pass
        try:
            self._close_panel()
        except Exception:
            pass          # a stuck popover must not keep the app alive
        try:
            # First: a hotkey that fires into a half-torn-down app is a crash,
            # and AppKit keeps every monitor alive until removeMonitor:.
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            self.midi.shutdown()
            self.engine.stop()
        except Exception:
            pass
        rumps.quit_application()

    def _confirm_quit_while_busy(self) -> bool:
        """Quit was asked for while a WAV render is in flight. May it proceed?

        The first ask is refused, because quitting now throws the render away:
        `recorder.export_wav` writes to a `<name>.part` sibling and only publishes
        it with one `os.replace` at the very end (and unlinks it if anything goes
        wrong), so the file the user picked in the save dialog is never created or
        truncated. There is nothing half-written to salvage and nothing to play -
        the export simply has to be done again. But Quit is the *only* route out
        of an app built with `quit_button=None`, and `_busy` is cleared by the
        worker's `finally` alone - a thread wedged writing to a stalled volume
        would otherwise leave Force Quit as the only exit. So the refusal says how
        to insist, and asking again offers it as a confirmation with the
        consequence spelled out.
        """
        if not self._quit_refused_while_busy:
            self._quit_refused_while_busy = True
            macui.alert("Please wait", f"Still {self._busy}… The audio is written "
                                       "to a temporary file and only saved once it "
                                       "is finished, so quitting now would leave "
                                       "no file at all.\n\nChoose Quit again if "
                                       "you want to quit anyway.")
            return False
        window = rumps.Window(
            title="Quit while exporting?",
            message=(f"Just Piano is still {self._busy}. Quitting now discards the "
                     "render: no audio file is left behind, so the export has to "
                     "be done again."),
            ok="Quit Anyway", cancel="Keep Waiting", dimensions=(0, 0),
        )
        return bool(window.run().clicked)

    # ------------------------------------------------------------------- tick
    def _tick(self, _timer) -> None:
        # Menu bar title
        self.title = self._menu_bar_title()

        # Status line
        if self._busy:
            status = f"⏳ {self._busy.capitalize()}…"
        elif self.bank.error and not self.bank.ready:
            # A failed render sets bank.error and leaves `ready` False for good:
            # without this the status line would sit on a frozen
            # "Building piano… N%" for the rest of the session.
            status = f"⚠️ Piano samples failed: {self.bank.error}"
        elif not self.bank.ready:
            status = f"⏳ Building piano… {int(self.bank.progress * 100)}%"
        elif not self._audio_started:
            status = f"⚠️ No audio output ({self.engine.error or 'unavailable'})"
        elif self.settings.error:
            # `Settings.error` is every problem the settings file has, not only a
            # refused write: a file that could not be read, one holding somebody
            # else's JSON, and a stored value that had to be replaced by its
            # default all land here too (see config.Settings). "not saved" would
            # be wrong for all three, and none of them has anywhere else to
            # surface - there is no window and no console.
            status = f"⚠️ Settings: {self.settings.error}"
        elif self.midi.port_name:
            status = f"● {self.midi.port_name} · {self.engine.latency_ms:.0f} ms"
        elif self.midi.error:
            # A port claimed by another app fails silently otherwise: open()
            # records why here and the watcher stops retrying it, so without
            # this the user only sees "Not connected" with no explanation.
            status = f"⚠️ {self.midi.error}"
        elif self._ports:
            status = "○ Not connected — pick a keyboard below"
        else:
            status = "○ Plug in a MIDI keyboard"
        if self._notes_played and self.bank.ready:
            status += f" · {self._notes_played} notes"
        # Muted is a state the user has to be able to confirm in words, not only
        # from the icon - and the words are also where "recording still works"
        # can be said. Never in front of a warning: a dead audio device is the
        # more urgent thing to read, and muting does not explain it.
        if self.engine.muted and not status.startswith(("⚠️", "⏳")):
            status = f"{ICON_MUTED} Muted (MIDI and recording still live) · {status}"
        self.status_item.title = status

        # Accessibility can be granted while the app runs. Only ever asked while
        # a hotkey is armed, event monitors exist at all *and* the global half is
        # missing, so the normal case never touches the API; the re-arm is
        # one-shot (see `_apply_hotkey`), and the asking itself is bounded by
        # TRUST_POLL_LIMIT so a permission that stays refused costs a couple of
        # minutes of polling rather than one call every half second for as long as
        # the app is up.
        if (self.hotkeys.hotkey is not None and hotkey.available()
                and not self.hotkeys.global_installed
                and self._trust_polls < TRUST_POLL_LIMIT):
            self._trust_polls += 1
            trusted = hotkey.trusted()
            if trusted is True and self._hotkey_rearm:
                self._apply_hotkey(self.settings["mute_hotkey"], rearm=False)
            elif trusted != self._trust_sig:
                self._refresh_hotkey_state()

        # Keep the device list and record labels fresh
        signature = (tuple(self._ports), self.midi.port_name)
        if signature != self._midi_sig:
            self._midi_sig = signature
            self._rebuild_midi_menu()
        self._refresh_take_state()

        # Report a finished WAV render here, on the main thread (AppKit).
        result = self._export_result
        if result is not None:
            self._export_result = None
            title, message, subtitle, path = result
            if path is None:
                # Failure: a Notification Center banner can be suppressed, and
                # the MIDI path already reports the same thing modally.
                macui.alert(title, message)
            else:
                macui.notify(title, message, subtitle)
                macui.reveal_in_finder(path)


def main() -> None:
    JustPianoApp().run()
