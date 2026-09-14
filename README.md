# 🎹 Just Piano

A macOS **menu bar** piano — five instruments. Plug in a MIDI keyboard and
play: no DAW, no window, no setup. Hit record, play, export a `.mid` (or a
`.wav`).

Three of them are **recorded** — a Yamaha C5 grand, a Kawai upright, and a felt
piano voiced from the grand's own recordings. The Rhodes and the Wurlitzer are
**synthesised** from a physical model (`justpiano/tone.py`) and carry no audio at
all. Everything ships in the app: 26 MB of FLAC, nothing to download.

```
🎹  ← lives here, in your menu bar
├── ● Nord Stage 3 · 6 ms · 213 notes            ← status, not clickable
├── ─────────────
├── MIDI Keyboard      ▸  your ports / Connect Automatically / Rescan Devices
├── Sound              ▸  Instrument / Volume / Reverb / Touch Response / Key Noise / Latency / Audio Output
├── ─────────────
├── Mute                                         ← ⌃⌥⌘M, from anywhere
├── Mute Hotkey        ▸  Off / ⌃⌥⌘M / ⌃⌥M / F13 · Global Access: Granted
├── ─────────────
├── Start Recording                              ⌘R
├── Take: 1:24 · 213 notes                       ← status, not clickable
├── Export MIDI File…                            ⌘E
├── Export Audio (WAV)…
├── Discard Recording
├── ─────────────
├── Export Everything Since Launch… (213 notes)
├── Open Recordings Folder
├── ─────────────
├── Panic (All Notes Off)
├── Open at Login
├── About Just Piano
└── Quit                                         ⌘Q
```

The four export/discard items are greyed out until there is something to save.

---

## Why it exists

Opening Logic or MainStage just to noodle for two minutes is absurd. Just Piano
starts in a second, sits in the menu bar, and is always ready. It also records
*everything* you play in the background — so the improvisation you didn't hit
record for isn't lost.

## Features

| | |
|---|---|
| **Instant play** | ~6 ms output latency (3 ms on the lowest setting). Auto-connects to your keyboard and reconnects when you re-plug it. |
| **Real piano tone** | Recorded stereo pianos: 4 velocity layers for the grand and felt, 2 for the upright, with the damper and pedal noise recorded separately. Every key that was not sampled reads a neighbour at most a semitone away — measured at 75–85 dB against an exact resample, so you cannot hear the join. |
| **The room** | Asymmetric early reflections in front of a Freeverb tail, so the instrument is somewhere rather than merely reverberant, and sympathetic resonance: press the damper pedal and the strings you did not play answer the ones you did. |
| **Five instruments** | Three acoustic pianos — **Grand** and **Upright** from recordings of two different instruments, **Felt** voiced from the grand — and two electric ones, **Rhodes** and **Wurlitzer**, synthesised as what they actually are: a struck steel bar seen through a non-linear pickup. Chosen from **Sound ▸ Instrument**; each is cached in its own right, so switching back to one you have played before is a file read. |
| **Key noise you control** | The damper landing when a key comes up, and the pedal mechanism, are separate recordings rather than part of the note. **Sound ▸ Key Noise** runs from Off to Prominent; the default is deliberately well under where the microphones put it. |
| **Full pedal support** | Sustain (CC64), sostenuto (CC66) and soft/una corda (CC67). All Sound Off (CC120), All Notes Off (CC123) and Reset All Controllers (CC121) behave the way the MIDI spec says: only CC121 lifts the pedals, and a held damper keeps notes ringing through CC123. |
| **Velocity + timbre** | Harder playing is louder *and* brighter, not just louder. Three touch curves. |
| **Recording** | Explicit takes, plus a rolling capture of everything since launch. |
| **Export** | Standard MIDI File (type 0, 480 PPQ at 120 BPM, pedal and pitch bend included) or a rendered 44.1 kHz 16-bit stereo WAV. |
| **Reverb** | Off / Room / Concert Hall. |
| **Virtual MIDI port** | Other apps and DAWs can play Just Piano through the `Just Piano` port. |
| **On-screen keyboard** | Click the menu bar icon for all 88 keys: they light up as you play, and you can click or drag across them to play without a MIDI keyboard. |
| **One-click mute** | Silence the piano without quitting it — for when the same keyboard is also driving a DAW. MIDI, recording and the key lights stay live; the menu bar shows a muted keyboard icon. Toggle it from the menu, the keyboard panel or a global hotkey (⌃⌥⌘M by default). |
| **Light** | No sample library to download, and none in the app: an instrument is 32–48 MiB of audio your Mac renders itself, once, on first use, and caches. Three of them are kept on disk at a time. |

---

## Install

You need macOS 11+ and Python 3.9+ (`python3 --version`; if it's missing, run
`xcode-select --install`).

### Option A — build the app (recommended)

**Double-click `Build Just Piano.command` in Finder.** That is the whole thing:
it builds the app, copies it to `/Applications`, and launches it — look for the
🎹 in your menu bar. A Terminal window opens to show the progress and waits for
Return before closing, so a failure stays on screen instead of vanishing.

*(macOS may refuse to run a `.command` it thinks came from the internet. If
nothing happens, right-click it ▸ **Open** once, or run
`chmod +x "Build Just Piano.command"` in the folder.)*

Same thing from a terminal, with the other flags:

```bash
cd Just Piano
./build_app.sh              # build dist/JustPiano.app
./build_app.sh --install    # …then copy it to /Applications and launch it
./build_app.sh --dmg        # …then package dist/JustPiano-1.0.0.dmg
./build_app.sh --help
```

The flags combine (`--dmg --install`).

With `--install` the app is copied to `/Applications` and launched (quitting any
copy that is already running first). Look for the 🎹 in your menu bar. Then turn
on **Open at Login** from the menu if you want it always there — macOS asks once,
at that moment, for permission to control System Events, which is how the login
item is added. The global **mute hotkey** wants one more permission
(Accessibility) before it can fire while another app has the focus; the
**Mute Hotkey** submenu takes you to the pane.

Without `--install` the bundle is left in `dist/` for you to drag over yourself.

**Sharing it.** `--dmg` writes `dist/JustPiano-<version>.dmg`, where the version
comes from `justpiano/__init__.py` (currently 1.0.0) — the same single source of
truth the PyInstaller spec reads. It's a zlib-compressed read-only (UDZO) image
holding the app beside a symlink to `/Applications`, so opening it gives the
usual "drag the icon onto the Applications folder" window. Both the app and the
image are signed ad-hoc, which is what lets the app launch on Apple Silicon —
but ad-hoc is not a paid Apple Developer ID, so anyone who downloads the `.dmg`
from the internet will get a Gatekeeper warning. Their first launch needs
right-click ▸ **Open**, or `xattr -cr /Applications/JustPiano.app`. Worth saying
up front when you send the file to someone (see
[Troubleshooting](#troubleshooting)).

### Option B — run from source

```bash
cd Just Piano
./run.sh
```

First run creates a virtualenv and installs dependencies (and reinstalls them
whenever `requirements.txt` changes; a half-built `.venv` is thrown away and
rebuilt). Quit from the menu, or press `Ctrl-C` in the terminal.
**Open at Login** is disabled when running from source — it needs the `.app`.

> **First launch spends a second or two** rendering the piano
> (the menu bar shows `🎹 42%`). The middle of the keyboard is rendered first,
> so you can start playing before the extremes are finished. The result is
> cached in `~/Library/Application Support/JustPiano/cache` (32–48 MiB for the
> instrument you are playing), and every launch after that maps the cache file
> instead of rendering — no progress counter, no pause you would notice. The
> samples are memory-mapped rather than read into RAM, so they cost the app
> nothing it is not currently playing, and the OS can reclaim them under
> pressure.
>
> The figures behind that: 1.43 s to render the bank and 0.7 ms to map it back,
> both measured on the Linux machine the profiling was done on (a 64-core EPYC,
> three worker threads — `docs/PERF_FINDINGS_2026-09-01.md`). That report says
> plainly that Mac cores are 2–3× slower, so treat the render as a few seconds at
> worst and the load as instant. Neither has been timed on a Mac.

---

## Using it

**Open it.** Click the menu bar icon: an 88-key keyboard drops down under it.
Keys light up as you play them, and you can click the keys themselves to play
without a MIDI keyboard — drag across them to glissando, and click lower down a
key to hit it harder. Click the **speaker** button in the corner to mute (see
below), or the **⚙︎** beside it (or right-click the menu bar icon) for the menu
with everything else in it. Clicking the icon again puts the keyboard away, and
so does clicking anywhere else.

**Play.** Plug in any USB/Bluetooth MIDI keyboard. Just Piano finds it
automatically — the menu shows `● Your Keyboard · 6 ms`. If you have several,
pick one under **MIDI Keyboard**; the choice is remembered, and Just Piano then
waits for exactly that device instead of switching to another one.
**Connect Automatically** puts it back on auto-pilot.

**Pick an instrument.** **Sound ▸ Instrument** is the first thing in the Sound
menu, and it offers five whole instruments rather than a bank of knobs:

| | |
|---|---|
| **Grand Piano** | The default. A nine-foot concert grand: bright, open, long in the bass. |
| **Upright Piano** | Not a quiet grand — a *shorter* one. Its bass strings were cut to fit the case, so their partials are stretched three times as far (that clangy, fundamental-poor bottom octave); its small board gives up radiating an octave higher and leans on the midrange instead; and its spring-return action is audibly busier. |
| **Felt Piano** | A moderator strip between hammer and string. The strings come down ~13 dB and go dark, which leaves the action unusually present — soft, intimate, close-miked. |
| **Rhodes (electric)** | A steel tine struck by a neoprene tip, its tonebar beating slowly against it, read by a magnetic pickup. Nearly a sine once the bell has gone; dig in and the bar's bending modes bark. |
| **Wurlitzer (electric)** | A lead-loaded reed swinging inside an electrostatic pickup at 170 V. Hollow and reedy where the Rhodes is glassy — odd harmonics lead — much shorter in the sustain, and it *barks*: play hard and the pickup saturates. |

A switch is a re-render rather than an EQ: the audio stops for a moment, every
sounding note is dropped, and the first time you choose an instrument the status
line counts through `⏳ Building piano… N%` while it is built in the background.
After that each one has its own cache file, so switching back is a file read.
The choice is remembered in `settings.json`. Re-picking the instrument you are
already playing costs nothing — unless its build failed, in which case it is the
retry. You cannot switch during a WAV export: Just Piano says *Please wait*
rather than swapping the piano out from under the render.

**Mute.** **Mute** silences the piano without quitting it — for when the same
keyboard is also playing a DAW and you don't want to hear both. It fades out over
8 ms, so there is no click, and the menu bar icon becomes a slashed keyboard
(🔇 in a checkout that has never run `tools/make_icon.py`). Everything except the
sound keeps running: notes still arrive, keys still light up on the on-screen
keyboard, and **recording still works** — you can record a whole take in silence
and export the `.mid` or the `.wav` afterwards. The status line says so:
`🔇 Muted (MIDI and recording still live)`. Unmuting is instant and never
re-opens the audio device.

There are three ways to toggle it, and they always agree with each other: the
**Mute** menu item (which checkmarks itself and reads *Muted*), the speaker
button in the corner of the keyboard panel, and the global hotkey. Mute itself is
deliberately *not* remembered across launches — an app that starts silent looks
broken.

**The hotkey.** **Mute Hotkey ▸** offers a short list rather than a shortcut
recorder: **Off**, **⌃⌥⌘M** (the default), **⌃⌥M** and **F13**. The choice *is*
remembered, in `settings.json`. Three modifiers plus a letter is not a shape
Logic, Ableton or FL ships, so the default won't fight the DAW you are playing;
⌃⌥M is the lighter alternative if ⌘ is awkward on your keyboard, and F13 is for
full-size keyboards with nothing bound to it. The hotkey never swallows the key
press — whatever is listening for it still gets it.

**Permission for the hotkey.** macOS only lets an app see key presses that went
to *another* app once it is trusted for Accessibility, and that is exactly the
half of the hotkey that matters while your DAW has the focus. Just Piano installs
both halves and degrades gracefully: until the permission is granted the hotkey
works while Just Piano itself has the focus, and everything else — the menu item
and the panel button — is unaffected. The last line of the **Mute Hotkey** submenu
always says where you stand:

| | |
|---|---|
| `Global Access: Granted` | the hotkey works from any app |
| `Allow Accessibility Access…` | click it: it opens **System Settings ▸ Privacy & Security ▸ Accessibility**, where you switch **Just Piano** on |
| `Open Accessibility Settings…` | macOS wouldn't answer; the same pane, worth a look |
| `Global Access: Not Needed` | the hotkey is **Off** |
| `Global Access: Unavailable` | running from source without pyobjc, so there are no key monitors at all |

Grant it once and Just Piano picks it up while it is running — no relaunch. It
never *prompts* for the permission by itself: that dialog would come back on
every launch. Add `/Applications/JustPiano.app` to that pane by hand if you'd
rather not go through the menu. Running from source, the trusted app is your
terminal (or Python), not Just Piano, which is one more reason to build the `.app`.

**Record.** Click **Start Recording** (or `⌘R` while the menu is open). The menu
bar icon turns into `⏺ 1:24`. Click again to stop. Starting a second recording
asks before replacing the take you already have. **Discard Recording** throws
the take away without exporting it.

**Export.** **Export MIDI File…** (`⌘E`) writes a Standard MIDI File with your
exact timing, velocities and pedal movements — drop it into Logic, Ableton,
MuseScore, anything. Notes and a sustain pedal still held at the end are closed
off for you. **Export Audio (WAV)…** re-renders the take to 44.1 kHz stereo
audio with a three-second tail so the last chord can ring out. Both open a
normal save dialog, default to `~/Music/JustPiano/`, and reveal the finished
file in the Finder. **Open Recordings Folder** opens that folder directly. An
export that fails — a full disk, a render that raises, a quit part-way through —
leaves no file at all: the audio goes to a `<name>.wav.part` sibling and is only
renamed into place once it is complete, so you never get a truncated WAV under
the name you picked, and a file that was already there is not clobbered by a
render that never finished.

**Forgot to hit record?** **Export Everything Since Launch…** saves every note
played since the app started.

### Settings

| Menu | Options |
|---|---|
| **Instrument** | Grand Piano (default, sampled) · Upright Piano (sampled) · Felt Piano (sampled) · Rhodes (synthesised) · Wurlitzer (synthesised) |
| **Key Noise** | Off · Subtle · Natural (default) · Prominent — the recorded action. Disabled for the synthesised instruments, which carry their mechanical noise inside the note. |
| **Volume** | 25 · 50 · 75 (default) · 100 · 125 % |
| **Reverb** | Off · Room (default) · Concert Hall |
| **Touch Response** | Light (easier to play loud) · Normal (default) · Heavy (more dynamic range) |
| **Latency** | Lowest ~3 ms · Balanced ~6 ms (default) · Safest ~12 ms — drop to *Safest* if you hear crackling |
| **Audio Output** | System default, or any specific interface |
| **Mute Hotkey** | Off · ⌃⌥⌘M (default) · ⌃⌥M · F13 |

Settings live in `~/Library/Application Support/JustPiano/settings.json`.
Mute itself is not among them — it is a state, not a preference, and every launch
starts audible.

**A broken settings file does not stop the app.** Every stored value is checked
on the way in: one that this build cannot use (a volume of 9.5, an instrument that no
longer exists, a blocksize of 0) is replaced by its default and named in the
status line — `⚠️ Settings: volume=9.5 is not a gain between 0 and 1.25; using
0.75 instead`. A file that cannot be read at all — invalid JSON, a list instead
of an object, somebody else's settings — starts Just Piano on the defaults, says
so in the same place, and is kept beside itself as `settings.json.bak`, because
the first menu click you make would otherwise overwrite the file you were trying
to fix.

### The status line

The first item in the menu always says what the app is doing:

| | |
|---|---|
| `● Nord Stage 3 · 6 ms` | connected and ready (`· 213 notes` once you play) |
| `🔇 Muted (MIDI and recording still live) · …` | muted; the rest of the line carries on as usual |
| `○ Not connected — pick a keyboard below` | ports exist, none of them open |
| `○ Plug in a MIDI keyboard` | nothing to connect to |
| `⏳ Building piano… 42%` | first launch, still rendering |
| `⏳ Rendering audio…` | a WAV export is in flight |
| `⚠️ Piano samples failed: …` | the render died; the app has no sound |
| `⚠️ No audio output (…)` | PortAudio refused every device |
| `⚠️ Could not open "…": …` | the keyboard is there but something else is holding it |
| `⚠️ Settings: …` | `settings.json` could not be read, or held a value that had to be replaced — including a write that failed, so your choices won't stick |

---

## Troubleshooting

**"Apple could not verify Just Piano is free of malware."**
The app is signed ad-hoc, not notarized. Right-click it in `/Applications` →
**Open** → **Open**, once. Or: `xattr -cr /Applications/JustPiano.app` —
extended attributes are not part of the seal, so this does not break the
signature.

**No sound.** Check the menu bar icon first: a slashed keyboard (or 🔇) means
Just Piano is muted — click **Mute** again, hit ⌃⌥⌘M, or use the button on the
keyboard panel. Otherwise check the status line at the top of the menu.
`⚠️ No audio output` means PortAudio couldn't open a device — pick a specific one
under **Sound ▸ Audio Output**. (A chosen device that fails falls back to the
system default automatically.) If the status shows your keyboard but nothing
sounds, try **Panic (All Notes Off)** and check your Mac's output volume.

**The mute hotkey only works when Just Piano is in front.** That is the missing
Accessibility permission, and nothing else: **Mute Hotkey ▸ Allow Accessibility
Access…** opens the pane where you switch **Just Piano** on. It takes effect while
the app is running. If the line reads `Global Access: Unavailable` you are running
from source without pyobjc installed — `./run.sh` installs every pyobjc framework
the app imports, including `pyobjc-framework-ApplicationServices`, which is where
`AXIsProcessTrusted` lives. If the shortcut collides with something else, pick
another preset (or **Off**); the menu item and the panel's button never need
permission.

**Keyboard not detected.** Use **MIDI Keyboard ▸ Rescan Devices**. Bluetooth
keyboards must be paired in *Audio MIDI Setup ▸ MIDI Studio* first. Note that
loopback ports (IAC Driver, Network Session) and Just Piano's own virtual port
are never auto-selected — pick them manually if that's what you want. If a port
is listed but won't connect, another app is probably holding it open: the status
line says `⚠️ Could not open "…": …` with the reason CoreMIDI gave. Auto-connect
then steps over that port for about 30 seconds and tries it again, so quitting
the other app gets your keyboard back on its own — no rescan, and the device list
does not have to change first. (A keyboard you picked by hand is retried every
tick, not held off.)

**Crackling / dropouts.** Set **Sound ▸ Latency ▸ Safest**.

**Notes hang.** **Panic (All Notes Off)** kills every voice, lifts the pedal
latches and drops the reverb tail, instantly — and without the click that
dropping the voices outright used to make. The same goes for an incoming All
Sound Off (CC120): the cut is a 1.2 ms fade rather than a dropped voice list,
over in about 10 ms, so it is perceptually immediate but not a step in the
waveform.

**"The piano samples are still being built."** A WAV export needs the whole
instrument, and on first launch — or just after you switch instrument — it isn't
there yet. Wait for the status line to stop counting, then export again.

**An export that won't finish.** The status line sits on `⏳ Rendering audio…`
and Quit is the only way out of a menu-bar-only app. Choosing **Quit** during a
render is refused once, with the reason; choose it again and you are offered
**Quit Anyway** against **Keep Waiting**. Quitting discards the render — there is
no half-written file to salvage — but a wedged export can never make Just Piano
unquittable.

**Open at Login won't stay on.** It drives System Events, so Just Piano needs
Automation permission: **System Settings ▸ Privacy & Security ▸ Automation ▸
Just Piano ▸ System Events**. Until it's granted you get a *"Could not update
login items"* alert. The item is unavailable altogether when running from
source, where it reads *Open at Login (build the .app first)*.

**Rebuild the piano.** Delete `~/Library/Application Support/JustPiano/cache`
and restart. The cache tidies itself two ways without being asked. It invalidates
itself whenever the tone model changes, and every time a bank loads or finishes
building Just Piano deletes the files this build can no longer read — otherwise
an update would leave the whole ~198 MiB of a five-instrument cache behind for
ever. Separately, only the **three** most recently played instruments keep a
cache at all: perfectly readable banks are never removed by the first rule, so
without the second one, browsing the Instrument menu once would cost a fifth of a
gigabyte permanently. Loading a bank counts as playing it, so the instrument you
keep coming back to is never the one evicted.

---

## How it works

```
MIDI keyboard ──▶ midi_in.py ──▶ synth.py ──▶ CoreAudio
   (CoreMIDI)         │        (voice mixer +
                      │         reverb.py, fed by
                      │         samplebank.py)
                      └──────▶ recorder.py ──▶ .mid  (mido)
                                            └─▶ .wav  (offline re-render)
```

| File | Role |
|---|---|
| `justpiano/tone.py` | The two synthesis engines and the five instruments over them: a struck string plus the soundboard that radiates it, and a struck bar seen through a non-linear pickup. Renders one note/velocity layer. |
| `justpiano/samplebank.py` | Renders all 88 × 2 layers of one instrument on a pool of up to three threads (middle of the keyboard first), streams the cache to disk as int16 (32–48 MiB), memory-maps it back, prunes caches this build can no longer read, and evicts the least recently played bank past the three it keeps. |
| `justpiano/synth.py` | Real-time polyphonic mixer: voice allocation, velocity cross-fade, pitch-based stereo, pedals, voice stealing, soft-clip limiter, ramped output mute. |
| `justpiano/reverb.py` | Freeverb, restructured so every delay line is longer than the processing chunk — the whole reverb is a handful of numpy slices instead of a per-sample loop. Each preset publishes the length of its own tail, so the engine can stop processing silence. |
| `justpiano/midi_in.py` | Port discovery, auto-connect, hot-plug watcher, virtual port. |
| `justpiano/recorder.py` | Event capture (two numpy `EventRing`s) and both exporters; the WAV is rendered to a `.part` sibling and published with one rename. |
| `justpiano/tray.py` | The menu bar UI (rumps). |
| `justpiano/keyboard.py` | The on-screen keyboard as pure logic: key geometry, hit-testing, velocity from the click position, which keys are lit. No AppKit, so it is fully testable headless. |
| `justpiano/keyboardview.py` | The AppKit half: the view that paints those keys, the popover under the menu bar icon, the mute button, and the status-item click that opens it. |
| `justpiano/hotkey.py` | The global mute hotkey: spec parsing, the `NSEvent` global + local monitors, and the Accessibility state behind them. Degrades to nothing when pyobjc isn't there. |
| `justpiano/config.py` | Application paths (`JUSTPIANO_HOME`, support/cache/recordings) and persisted settings, validated on the way in and on every assignment. |
| `justpiano/macui.py` | Native macOS glue: save panel, alerts, notifications, Finder reveal, login item, System Settings panes. |
| `tools/selftest.py` | Headless engine/DSP/export test suite. |
| `tools/tray_smoke.py` | Headless integration test of the whole app, with stubbed macOS APIs. |
| `tools/demo_render.py` | Renders a demo performance to WAV + MIDI through one instrument (or `all` of them, side by side), no hardware needed. |
| `tools/make_icon.py` | Draws `assets/icon.png` for the app bundle and `assets/icon-muted.png` for the menu bar. |
| `Build Just Piano.command` | Double-click in Finder: builds the app, installs it to `/Applications`, launches it. A wrapper over `build_app.sh --install`. |
| `Commit and Push.command` | Double-click in Finder: shows what changed, asks for a commit message, commits and pushes the current branch. Cancels on an empty message, and never stages anything before showing it to you. |
| `Ship a Release.command` | Double-click in Finder: checks the repository is fit to release, then pushes a version tag. GitHub Actions builds the `.app` on a real Mac and publishes the disk image. Nothing is uploaded from your machine. |
| `justpiano/sampled.py` | The sampled instruments: pack manifests, the note-to-recording mapping, and the decode-once-then-memory-map cache. |
| `justpiano/resonance.py` | Sympathetic resonance — one tuned resonator per undamped string, under the damper pedal. |
| `assets/samples/` | The recordings, one directory per pack, each with a `pack.json` saying which recording covers which keys at which velocity. Adding an instrument is dropping in a folder. |

**Why both?** The acoustic pianos are recorded and the electric ones are
synthesised, and that split is not a compromise — it is where each approach is
honest.

An acoustic piano is 230 strings on one bridge over one soundboard, radiating
into a room. Modelling it produced something measurably wrong in a way no
parameter fixed: over a six-note chord the synthesised grand came out at 0.994
L/R correlation with its side channel 25 dB down, which is a mono signal. The
same measurement on the recordings gives correlation between +0.3 and −0.35 and
a side channel within 3 dB of the middle. That is 25 dB of stereo image that no
amount of partial-series work was going to supply.

An electric piano is a struck steel bar in front of a pickup — a small enough
system to model honestly, and one where the model gives you something a sample
cannot: the bark comes out of the pickup non-linearity at play time, so it
tracks how hard you hit rather than crossfading between two recordings of it.

The synthesis engine in `tone.py` is still complete and still renders all five
instruments; it is what you hear if the sample packs are missing. Both engines
come down to the same kernel — add up a few hundred decaying,
slightly detuned sinusoids — and differ entirely in where the numbers come from.

**The struck string** (Grand, Upright, Felt). Partials follow
`f_k = k·f₀·√(1 + B·k²)` with `B` sweeping 3e-5 in the bass to 3e-2 in the
treble, scaled per instrument: an upright's is 3.2× a grand's at A0 and 1.25× at
C8, because it is the *bass* strings that had to be cut to fit the case. The
series is cut by *frequency* — 92 % of Nyquist, so A0 gets all 345 of its
partials and the bass is as bright as the rest of the instrument. Hammer strike
position produces the `|sin(πkp)|` comb, blended against a floor so the
fundamental survives (a real soundboard restores it). Each partial gets its own
T60, so the tone darkens as it decays — and each one stops being rendered once
its envelope drops below -80 dB. A note is a unison choir, as on the instrument:
one string up to E1, two to B2, three above that, all sharing a launch phase —
one hammer hits them together — and detuned 0.5 to 2.0 cents apart, which is
where the shimmer comes from. Even a single bass string beats, its two
polarisations a few tenths of a cent apart.

The strings are then heard through the body: soundboard modes near 50/80/120 Hz
(and more wood above that), plus a 12 dB/octave radiation roll-off below a corner
around 200 Hz (300 for the smaller upright board), which is why A0's fundamental
is not its loudest partial. Hammer, key-bed and damper noise go through a 280 ms
synthesised body impulse response on top.

**The struck bar** (Rhodes, Wurlitzer). An electric piano is not a string
instrument, and modelling it as one is why so many synthesised Rhodes patches
sound like a filtered organ. What vibrates is a stiff cantilever, and what you
hear is a pickup's non-linear view of it:

- **Bending modes, not harmonics.** A clamped-free bar rings at
  1 : 6.267 : 17.55 : 34.4 — wildly inharmonic. Those upper modes *are* the tine
  bark; they are what a DX7 E.PIANO patch imitates with a modulator at ratio 18,
  and they die in a few hundred milliseconds while the fundamental sings on. On a
  hard blow the first one sits ~15 dB under the fundamental; on a soft one, ~29.
  The Wurlitzer's reed carries a blob of lead solder on its tip, which is exactly
  what kills its bending modes — so its bark comes from the pickup instead.
- **A harmonic series generated by the pickup, not by the bar.** Harmonic *n*
  comes from the n-th order term of the pickup's transfer, so its amplitude goes
  as the n-th power of the excursion — which is why the same `T60 ~ 1/f` law
  makes harmonic *n* decay *n* times faster, and why an electric piano audibly
  purifies towards a sine as it rings. A Rhodes' series is steep (-15.5 dB/oct);
  a Wurlitzer's is "closer to a sawtooth" (-9.5) with the odd harmonics 8 dB
  above the even ones, which is the hollow, reedy part of its character.
- **A tuned partner.** A Rhodes tine is one arm of an asymmetric tuning fork —
  the tonebar beside it is the other, tuned to the same pitch and never quite
  reaching it — so every note breathes at a fraction of a hertz.
- **The pickup itself.** An asymmetric term (`x + a·x²`, DC-blocked afterwards)
  for the way the magnetic field or the capacitive gap falls off across the
  swing, which is the even-harmonic "bloom" on the attack; then a symmetric
  saturation for the overdrive. Gentle on a Rhodes. On a Wurlitzer, whose
  electrostatic pickup works across a few thousandths of an inch at 170 V, it is
  the bark itself. Both are scaled by velocity, so a whisper stays clean.

---

## Testing

Both suites run headless on any machine — no audio hardware, no MIDI keyboard,
no macOS required.

```bash
python3 -m tools.selftest     # 379 checks: DSP, engine, pedals, mute ramp, reverb, instruments, exporters, key geometry, menu bar art, memory footprint
python3 -m tools.tray_smoke   # 295 checks: full app with stubbed macOS APIs
python3 -m tools.demo_render out.wav        # audition the default instrument
python3 -m tools.demo_render out.wav all    # ...or all five, side by side
```

`tray_smoke` replaces rumps/PortAudio/CoreMIDI with stubs that mirror the real
APIs, then boots the actual app: builds the menu, plays notes through the audio
callback, records, exports, switches settings, fails an audio device on purpose
to prove the fallback works, cancels dialogs, simulates unplugging the keyboard,
checks that two identically named keyboards stay individually selectable, mutes
from all three entry points and records a take in silence, denies and then grants
Accessibility under the hotkey, switches instrument under a held pedal and back to a
cached one, and walks through the failures a user has to be told about — a refused
MIDI port, a sample bank that never built, an instrument whose build failed, a WAV
render that failed, settings that won't save, a muted icon that was never
rendered. The stubs are deliberately never *more* permissive than the real
libraries — a stub that cannot fail is how a real-Mac crash slips through a green
run.

**Mutation testing, honestly.** Individual checks have been validated by breaking
the production code they cover and confirming the failure, but that has never been
done for all 674 of them, and an audit of the suites found six mutations they
survived at the time: a renamed AppKit selector on the status-item button, an
emptied `ICON_MUTED`, a `latency_ms` that returned a constant, a `to_pcm16` that
dropped its clip, a different cut of the partial series, and a menu bar icon
rendered at the wrong size. All six now go red — `tray_smoke` catches the first
three, `selftest` the last three, each re-run against the current suites on a
scratch copy of the tree. Read the counts as coverage, not as proof that every
check can fail.

Both suites create a fresh throwaway `JUSTPIANO_HOME` per run and delete it
afterwards, so they never touch (or read back) your real settings, cache or
recordings. `demo_render` uses a reusable `justpiano-demo` folder under your temp
directory.

---

## Known limits

- Pitch bend and the modulation wheel are **recorded** to MIDI but don't affect
  the audio (the engine plays fixed-pitch samples).
- MIDI channels are merged in the audio — everything plays the piano — although
  the exported file keeps each event's original channel.
- Program changes and aftertouch are ignored completely: neither played nor
  recorded. It's a piano.
- Polyphony is capped at 64 voices; past that the oldest are faded out.
- One take at a time. Starting a new recording replaces the previous one.
- The mute hotkey is a short preset list (Off / ⌃⌥⌘M / ⌃⌥M / F13), not a shortcut
  recorder, and the global half needs Accessibility. Mute is not remembered
  across launches, on purpose.
- The since-launch buffer keeps the most recent 400,000 MIDI events (hours of
  playing) in a ~4.2 MiB numpy ring; beyond that the oldest event drops off as
  each new one arrives. It lives in memory only, so quitting clears it.
- Switching instrument stops the audio for a moment and drops whatever is
  sounding, and the first switch to one has to render it (a second or two). Only
  the three most recently played keep a cache on disk (~120 MiB at most), so
  coming back to a fourth after a long gap re-renders it.
- The electric pianos are the instrument, not the rig: no tremolo, no amp, no
  phaser. Send the virtual MIDI port at a DAW if you want those.
- No playback of recorded takes inside the app; export and open elsewhere.

## License

The **code** is proprietary: copyright © 2026, all rights reserved. No licence is
granted to use, copy, modify or redistribute it.

The **audio** in `assets/samples` is not mine to relicense: it is Alexander
Holm's, under CC-BY 3.0 (see Credits below). CC-BY allows commercial use and
modification — trimming and resampling it, as this project does, is fine — and
asks for attribution in return.

## Credits

Two sample packs, both redistributable, both credited whether or not their
licence asks for it.

**Grand and Felt** — [Salamander Grand Piano V3](https://archive.org/details/SalamanderGrandPianoV3)
by **Alexander Holm**, [CC-BY 3.0](http://creativecommons.org/licenses/by/3.0/).
A Yamaha C5 recorded at 44.1 kHz with two AKG C414s in an AB pair about 12 cm
above the strings. Attribution is a condition of that licence: if you fork this
and keep `assets/samples/salamander`, keep this credit.

**Upright** — [Upright Piano KW](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html#UprightKW)
by **Gonzalo** and **Roberto** of the FreePats project,
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain). A
Kawai upright in a living room, recorded with a Zoom H1 where the player's head
would be. CC0 asks for nothing; it is credited anyway.

Felt is the one derived instrument, and it is derived honestly: a moderator
strip is a piece of felt lowered between the hammers and the strings of the
*same* piano, so the grand's own recordings are the right source for it. The
upright is a recording of an actual upright rather than the grand with its
treble taken off — an upright's strings are short enough that its inharmonicity
is several times a grand's, and no equaliser adds inharmonicity.

### What ships, and why so little

26 MB of FLAC for three instruments. Only the recorded pitches are in the app:
30 for the grand (minor thirds, A0 up, four of the library's sixteen velocity
layers) and 33 for the upright, plus 88 chromatic release samples and the pedal
noises. Every other key reads a neighbour at a different rate, at most a
semitone away for the grand — measured against an exact windowed-sinc resample
of real piano samples, a semitone of the mixer's Catmull-Rom read lands at 75–85
dB SNR, past what 16-bit source material can carry.

Each pack decodes once into a memory-mapped cache, so the samples are pages the
OS can reclaim rather than 90 MB of heap.

The Rhodes, the Wurlitzer, and the whole synthesis engine behind them are
original work and belong to this project.
