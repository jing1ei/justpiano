"""Windows desktop frontend; audio, MIDI and recordings share the Mac engine."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import config, recorder, tone
from .keyboard import Keyboard, KeyboardController, draw_plan
from .midi_in import MidiInput
from .sampled import make_bank
from .synth import AudioEngine, list_output_devices

REVERBS = {"Off": "off", "Studio": "studio", "Room": "room",
           "Warm Chamber": "chamber", "Concert Hall": "hall", "Ambient": "ambient"}


class PianoWindow:
    def __init__(self, root):
        self.root = root
        self.closed = False
        self.busy = False
        self.quit_waited = False
        self.results = queue.SimpleQueue()
        self.settings = config.Settings()
        self.recorder = recorder.Recorder()
        self.bank = make_bank(self.settings["voicing"])
        self.engine = AudioEngine(self.bank, blocksize=self.settings["blocksize"],
                                  volume=self.settings["volume"],
                                  reverb_preset=self.settings["reverb"],
                                  velocity_curve=self.settings["velocity_curve"],
                                  strike_variation=self.settings["strike_variation"],
                                  resonance=self.settings["resonance"],
                                  key_noise=self.settings["key_noise"])
        self.keys = KeyboardController(on_note_on=lambda n, v: self.message(0x90, n, v),
                                       on_note_off=lambda n: self.message(0x80, n, 0))
        self.midi = MidiInput(self.message)
        self.midi.preferred = self.settings["midi_port"]
        root.title("Just Piano")
        icon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "icon.png")
        if os.path.isfile(icon_path):
            self.app_icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, self.app_icon)
        root.minsize(940, 340)
        root.geometry("980x360")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<KeyPress>", self.key_down)
        root.bind("<KeyRelease>", self.key_up)
        root.bind("<FocusOut>", lambda e: self.keys.release_inputs())
        body = ttk.Frame(root, padding=14)
        body.pack(fill="both", expand=True)
        controls = ttk.Frame(body)
        controls.pack(fill="x")
        self.instrument = self.combo(controls, "Instrument", list(tone.VOICING_LABELS.values()),
                                     tone.VOICING_LABELS[self.bank.voicing], self.pick_instrument)
        self.reverb = self.combo(controls, "Reverb", list(REVERBS),
                                 next(k for k, v in REVERBS.items() if v == self.settings["reverb"]),
                                 self.pick_reverb)
        self.touch = self.combo(controls, "Touch", ["soft", "normal", "hard"],
                                self.settings["velocity_curve"], self.pick_touch)
        volume = ttk.Frame(controls)
        volume.pack(side="left", padx=8)
        ttk.Label(volume, text="Volume").pack(anchor="w")
        ttk.Scale(volume, from_=0, to=1.25, value=self.settings["volume"],
                  command=self.set_volume).pack()
        devices = ttk.Frame(body)
        devices.pack(fill="x", pady=10)
        self.midi_choice = self.combo(devices, "MIDI input", [], "Automatic", self.pick_midi)
        self.output_choice = self.combo(devices, "Audio output", [], "System Default", self.pick_output)
        self.latency = self.combo(devices, "Buffer", ["128", "256", "512", "1024"],
                                  str(self.settings["blocksize"]), self.pick_latency)
        self.noise = self.combo(devices, "Key noise", ["Off", "Subtle", "Natural", "Prominent"],
                                min({"Off": 0, "Subtle": .25, "Natural": .5, "Prominent": 1},
                                    key=lambda n: abs({"Off": 0, "Subtle": .25, "Natural": .5, "Prominent": 1}[n] - self.settings["key_noise"])), self.pick_noise)
        ttk.Button(devices, text="Rescan", command=self.rescan).pack(side="left", padx=6, pady=18)
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.record_button = ttk.Button(actions, text="Record", command=self.toggle_record)
        self.record_button.pack(side="left")
        self.mute_button = ttk.Button(actions, text="Mute", command=self.toggle_mute)
        self.mute_button.pack(side="left", padx=5)
        ttk.Button(actions, text="Panic", command=self.panic).pack(side="left")
        self.exports = []
        for title, kind, source in (("Export MIDI", "mid", "take"), ("Export WAV", "wav", "take"),
                                     ("Export session", "mid", "session")):
            button = ttk.Button(actions, text=title,
                                command=lambda k=kind, s=source: self.export(k, s))
            button.pack(side="left", padx=5)
            self.exports.append((button, source))
        self.discard_button = ttk.Button(actions, text="Discard", command=self.discard)
        self.discard_button.pack(side="left")
        ttk.Button(body, text="Open recordings folder", command=self.open_recordings).pack(anchor="e", pady=(6, 0))
        self.status = ttk.Label(body, text="Loading piano…", wraplength=900)
        self.status.pack(anchor="w", pady=10)
        self.canvas = tk.Canvas(body, height=120, highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.resize)
        self.canvas.bind("<ButtonPress-1>", self.mouse_down)
        self.canvas.bind("<B1-Motion>", lambda e: self.keys.mouse_dragged(e.x, e.y))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.keys.mouse_up())
        ttk.Label(body, text="Click or drag the piano · A W S E D F T G Y H U J K O L P ; to play · Esc releases notes").pack(anchor="w", pady=(6, 0))
        self.rescan()
        self.start_audio()
        self.bank.load_or_build_async()
        self.midi.open(self.midi.preferred)
        self.midi.start_watcher()
        self.timer = root.after(30, self.tick)

    @staticmethod
    def combo(parent, title, values, value, callback):
        frame = ttk.Frame(parent)
        frame.pack(side="left", padx=(0, 10))
        ttk.Label(frame, text=title).pack(anchor="w")
        box = ttk.Combobox(frame, values=values, state="readonly", width=18)
        box.set(value)
        box.pack()
        box.bind("<<ComboboxSelected>>", callback)
        return box

    def message(self, status, d1, d2, when=None):
        kind = status & 0xF0
        if kind == 0x90 and d2:
            self.engine.note_on(d1, d2)
            self.keys.lights.press(d1, d2)
        elif kind == 0x80 or (kind == 0x90 and not d2):
            self.engine.note_off(d1)
            self.keys.lights.release(d1)
        elif kind == 0xB0:
            self.engine.control_change(d1, d2)
            if d1 in (120, 123):
                self.keys.lights.release_all()
        self.recorder.handle(status, d1, d2)

    def key_down(self, event):
        if event.keysym == "Escape":
            self.keys.release_inputs()
            return "break"
        if event.widget is not self.canvas or event.state & 0xC:
            return
        if self.keys.key_down(event.char):
            return "break"

    def key_up(self, event):
        key = ';' if event.keysym == 'semicolon' else event.keysym.lower() if len(event.keysym) == 1 else event.char
        if self.keys.key_up(key):
            return "break"

    def mouse_down(self, event):
        self.canvas.focus_set()
        self.keys.mouse_down(event.x, event.y)

    def resize(self, event):
        self.keys.release_inputs()
        self.keys.keyboard = Keyboard(max(1, event.width), max(1, event.height))
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        def color(rgb):
            return "#" + "".join(f"{round(c*255):02x}" for c in rgb)
        for op in draw_plan(self.keys.keyboard, self.keys.lit):
            if op.kind == "key":
                self.canvas.create_rectangle(op.x, op.y, op.x+op.width, op.y+op.height,
                                             fill=color(op.fill), outline=color(op.stroke))
            else:
                self.canvas.create_text(op.x+op.width/2, op.y+op.height-10,
                                        text=op.text, fill=color(op.fill), font=("Segoe UI", 8))

    def rescan(self):
        self.midi._open_failed.clear()
        ports = self.midi.list_ports()
        self.port_labels = {f"{i+1}: {name}": (i, name) for i, name in enumerate(ports)}
        if self.midi.preferred and self.midi.preferred not in ports:
            self.port_labels[f"Unavailable: {self.midi.preferred}"] = (None, self.midi.preferred)
        self.midi_choice["values"] = ["Automatic", *self.port_labels]
        self.midi_choice.set(next((k for k, (_, n) in self.port_labels.items()
                                   if n == self.midi.preferred), "Automatic"))
        self.outputs = {f"{i}: {name}": i for i, name in list_output_devices()}
        self.output_choice["values"] = ["System Default", *self.outputs]
        saved = self.settings["output_device"]
        self.output_choice.set(saved if saved in self.outputs else "System Default")

    def pick_midi(self, _event=None):
        selected = self.port_labels.get(self.midi_choice.get())
        name = selected[1] if selected else None
        self.midi.preferred = name
        self.settings["midi_port"] = name
        self.panic()
        self.midi.open(name, index=selected[0] if selected else None)
        self.canvas.focus_set()

    def start_audio(self):
        self.panic()
        device = self.outputs.get(self.output_choice.get())
        if not self.engine.start(device) and device is not None:
            self.engine.start(None)
            self.output_choice.set("System Default")

    def pick_output(self, _event=None):
        self.settings["output_device"] = (self.output_choice.get()
                                          if self.output_choice.get() in self.outputs else None)
        self.start_audio()
        self.canvas.focus_set()

    def pick_latency(self, _event=None):
        self.engine.blocksize = int(self.latency.get())
        self.settings["blocksize"] = self.engine.blocksize
        self.start_audio()

    def pick_noise(self, _event=None):
        value = dict(zip(("Off", "Subtle", "Natural", "Prominent"), (0, .25, .5, 1)))[self.noise.get()]
        self.engine.set_key_noise(value)
        self.settings["key_noise"] = value

    def pick_instrument(self, _event=None):
        name = next(k for k, v in tone.VOICING_LABELS.items() if v == self.instrument.get())
        if name == self.bank.voicing and self.bank.ready:
            return
        if self.busy or not self.bank.ready and not self.bank.error:
            self.instrument.set(tone.VOICING_LABELS[self.bank.voicing])
            return
        self.panic()
        try:
            bank = make_bank(name)
        except Exception as exc:
            self.instrument.set(tone.VOICING_LABELS[self.bank.voicing])
            messagebox.showerror("Could not load instrument", str(exc), parent=self.root)
            return
        self.engine.stop()
        self.bank = self.engine.bank = bank
        self.settings["voicing"] = name
        self.start_audio()
        bank.load_or_build_async()

    def pick_reverb(self, _event=None):
        name = REVERBS[self.reverb.get()]
        self.engine.set_reverb(name)
        self.settings["reverb"] = name

    def pick_touch(self, _event=None):
        self.engine.set_velocity_curve(self.touch.get())
        self.settings["velocity_curve"] = self.touch.get()

    def set_volume(self, value):
        self.engine.volume = float(value)
        if hasattr(self, "volume_save"):
            self.root.after_cancel(self.volume_save)
        self.volume_save = self.root.after(200, self.save_volume)

    def save_volume(self):
        self.settings["volume"] = self.engine.volume
        if hasattr(self, "volume_save"):
            del self.volume_save

    def toggle_mute(self):
        self.engine.set_muted(not self.engine.muted)
        self.mute_button.config(text="Unmute" if self.engine.muted else "Mute")

    def panic(self):
        self.keys.release_inputs()
        self.keys.lights.release_all()
        self.engine.all_notes_off(immediate=True)

    def toggle_record(self):
        if self.recorder.recording:
            self.recorder.stop()
        elif not len(self.recorder.take) or messagebox.askyesno(
                "Replace take?", "Start a new take and discard the current one?", parent=self.root):
            self.recorder.start()

    def discard(self):
        if self.recorder.recording or self.busy:
            return
        if len(self.recorder.take) and messagebox.askyesno(
                "Discard take?", "Discard the current recording?", parent=self.root):
            self.recorder.discard()

    def export(self, ext, source):
        if self.busy:
            return
        if source == "take" and self.recorder.recording:
            messagebox.showinfo("Stop recording first", "Press Stop, then export the completed take.", parent=self.root)
            return
        if ext == "wav" and not self.bank.ready:
            messagebox.showinfo("Sound is loading", "Wait for the instrument to finish loading, then export audio.", parent=self.root)
            return
        events = self.recorder.snapshot(source)
        if not events:
            return
        try:
            os.makedirs(config.RECORDINGS_DIR, exist_ok=True)
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension="."+ext,
                                                initialdir=config.RECORDINGS_DIR,
                                                initialfile=recorder.default_filename(ext=ext),
                                                filetypes=[(ext.upper(), "*."+ext)])
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.root)
            return
        if not path:
            return
        self.busy = True
        self.quit_waited = False
        bank = self.bank
        sound = {k: self.settings[k] for k in ("volume", "reverb", "velocity_curve",
                                               "strike_variation", "resonance", "key_noise")}
        def work():
            try:
                if ext == "wav":
                    recorder.export_wav(events, bank, path, **sound)
                else:
                    recorder.export_midi(events, path)
                self.results.put((True, path))
            except Exception as exc:
                self.results.put((False, str(exc)))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as exc:
            self.busy = False
            messagebox.showerror("Export failed", str(exc), parent=self.root)

    def tick(self):
        if self.closed:
            return
        while not self.results.empty():
            ok, result = self.results.get()
            self.busy = False
            if ok:
                messagebox.showinfo("Exported", result, parent=self.root)
            else:
                messagebox.showerror("Export failed", result, parent=self.root)
        if self.keys.refresh():
            self.draw()
        stats = self.recorder.stats()
        state = ("Exporting…" if self.busy else self.bank.error if not self.bank.ready and self.bank.error
                 else f"Loading {self.bank.progress:.0%}" if not self.bank.ready
                 else "Audio unavailable: " + str(self.engine.error) if self.engine.stream is None
                 else self.settings.error or self.midi.error or self.midi.port_name or "Play with your computer keyboard or connect MIDI")
        self.status.config(text=f"{tone.VOICING_LABELS[self.bank.voicing]} · {state} · {stats.take_notes} take notes"
                           + (f" · Recording {self.recorder.elapsed:.0f}s" if self.recorder.recording else "")
                           + (" · Muted" if self.engine.muted else ""))
        self.record_button.config(text="Stop" if self.recorder.recording else "Record")
        self.discard_button.config(state="normal" if len(self.recorder.take) and not self.recorder.recording and not self.busy else "disabled")
        for button, source in self.exports:
            count = len(self.recorder.take) if source == "take" else len(self.recorder.session)
            button.config(state="normal" if count and not self.busy
                          and (source == "session" or not self.recorder.recording) else "disabled")
        self.timer = self.root.after(30, self.tick)

    def open_recordings(self):
        try:
            os.makedirs(config.RECORDINGS_DIR, exist_ok=True)
            os.startfile(config.RECORDINGS_DIR)
        except Exception as exc:
            messagebox.showerror("Could not open recordings folder", str(exc), parent=self.root)

    def quit(self):
        if self.busy:
            if not getattr(self, "quit_waited", False):
                self.quit_waited = True
                messagebox.showinfo("Export in progress", "Wait for export to finish. If it is stuck, close again to cancel the export and quit.", parent=self.root)
                return
            if not messagebox.askyesno("Cancel export and quit?",
                                       "The unfinished export will not be saved. Quit anyway?", parent=self.root):
                return
        if (len(self.recorder.take) or len(self.recorder.session)) and not messagebox.askyesno(
                "Close Just Piano?", "The take and session are kept only until you close. Export first if needed.", parent=self.root):
            return
        if hasattr(self, "volume_save"):
            self.root.after_cancel(self.volume_save)
        self.save_volume()
        self.closed = True
        self.root.after_cancel(self.timer)
        self.midi.shutdown()
        self.panic()
        self.engine.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        PianoWindow(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("Just Piano could not start", str(exc), parent=root)
        root.destroy()
        raise
