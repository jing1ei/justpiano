# Just Piano cost report — historical snapshot, 1 September 2026

> **Read this as a record, not as a to-do list.** It is kept because it is the
> measurement behind several deliberate choices in the code (why the build pool is
> three threads, why the sample blob is memory-mapped, why the recorder is a numpy
> ring, why the cache is pruned) — the numbers are worth having, the instructions
> are not: every recommendation in its "Clear wins" and "Real trade-off" sections
> has since landed. (The "Dead weight" list was never acted on wholesale, and some
> of it — `NoteLights.__len__`, say — is still there.)
>
> Two consequences of that:
>
> * **Every line reference below is stale.** The files have all moved on. It cites
>   `tray.py:208 _on_bank_ready`, which no longer exists, and says "no `os.remove`
>   exists in `justpiano/`" when `samplebank.prune_cache()` is exactly that.
>   Treat the `file.py:NNN` citations as "somewhere in that file, on 1 Sep 2026".
> * **The timings are Linux/EPYC timings.** The header says it: Mac cores are
>   slower, and the report's own estimate is 2–3×. Nothing here has been measured
>   on a Mac.
>
> What landed, against the numbered items: 1 (mmap) in `samplebank._load_cache`;
> 2 (reverb silence gate) as `synth.REVERB_IDLE_MARGIN` + `Reverb.tail_frames`;
> 3 (session buffer) as `recorder.EventRing`; 4 (worker count) as
> `samplebank.MAX_BUILD_WORKERS = 3`; 5 (streamed cache write) as
> `samplebank._write_cache`; 6 (pruning) as `samplebank.prune_cache`, keeping the
> other voicings so switching back is still a file read.
>
> Since then the app has grown from three voicings to five instruments in two
> families (`grand`, `upright`, `felt`, `rhodes`, `wurlitzer`), so every cache
> figure below is a three-voicing one: the five come to ~198 MiB, and
> `samplebank.MAX_CACHED_BANKS` now caps what is kept at three of them, least
> recently played evicted first. The per-bank numbers (44.6 MiB for the grand,
> the mmap timings, the worker count) are unchanged.

---

## The report as written


Host: EPYC 9Y24, Linux, Python 3.11.9, numpy 2.4.6. Bank = grand, 88×2 layers, 44.1 kHz, int16, 23,378,986 samples = **44.6 MiB**. Callback budget @256 frames = 5.805 ms; Mac cores are slower, so callback %s are ~2–3× optimistic.

## Clear wins

**1. mmap the bank blob.** `samplebank.py:259` `np.load()` + `:277` views ⇒ 44.6 MiB of *dirty anonymous* RAM (anon 15.7→60.2 MiB). `np.load(p, mmap_mode='r').view(np.ndarray)` ⇒ anon stays 15.7 MiB, bank RSS becomes clean/file-backed/reclaimable; load 22–72 ms → **0.7 ms**; a real C3–C5 session faults in only **12.0 of 44.6 MiB**. Nothing defeats it: `render()` never writes samples (verified `writeable=False`), slices are contiguous, `astype` already copies. The `.view` matters — raw `np.memmap` slices cost 0.604 ms at 30 voices vs 0.338 (copy); with the view, 0.340, free. Risk: SIGBUS if the `.npy` is deleted mid-run (`os.replace` in `_save_cache` is safe).

**2. Reverb runs on silence.** `synth.py:414` → `reverb.py:130`. Idle, 0 voices: **0.189 ms/callback = 3.25 % of one core, forever** (`reverb="room"` is the default, `config.py:31`); off is 0.003 ms. Gate `process()` on "0 voices for longer than the tail": saves 3.2 % of a core 24/7. Risk: clipped tail if too eager.

**3. Session buffer.** `recorder.py:21` `SESSION_LIMIT=400_000`: at the limit the list costs **46.2 MiB RSS** — as much as the bank. Worse, the trim at `recorder.py:62-65` re-scans 100 k events: **8.7 ms on the rtmidi callback thread holding `_lock`** ⇒ MIDI timing hiccup. Use a numpy ring buffer (or ~50 k limit) and decrement the count incrementally. Saves ≤46 MiB and the stall. Risk: shortens "everything since launch".

**4. Too many build workers.** `samplebank.py:204` `min(8, cpu_count)`. Wall: 1w 2.67 s, 2w 1.68, **3w 1.43**, 4w 1.56, 8w 1.98, 12w 2.11 — the shipped 8 is **33 % slower** than 3, burns 6.6 s CPU vs 3.4 (GIL), and lifts build peak RSS to 139.5 vs 103.2 MiB. Use 3: saves 0.5 s wall, 3 s CPU, 36 MiB peak. Risk: none.

**5. `_save_cache` concatenates the whole bank.** `samplebank.py:232`: peak 130.5 vs 85.9 MiB steady = **+44.6 MiB** on first launch. Write the `.npy` header, then stream buffers. Risk: hand-written header.

## Real trade-off — your call

**6. Disk cache never pruned.** 44.6+44.6+37.9 = **127.1 MiB** for three voicings, and no `os.remove` exists in `justpiano/`, so every `BANK_VERSION`/`TONE_FINGERPRINT` bump orphans another 127 MiB forever. Prune stems not matching the current version; pruning *other voicings* costs the "switch back is a file read" property.

**7. Startup.** numpy 58 ms + justpiano 13 + construction 0.3 + warm bank 91 = **163 ms to usable** (rumps/pyobjc/PortAudio unmeasurable here). Item 1 removes ~70 ms; numpy can't be deferred.

## Not worth it — measured, already fine

- **int16 is right** (`samplebank.py:195`). float32 = 89.2 MiB, render *identical* (0.341 vs 0.336 ms @30v); quantisation floor −90.1 dBFS sits under `tone.py:140`'s −80 dB.
- **Callback headroom is huge.** @256: 0 voices 0.05 %, 10+reverb 4.2 %, 30+reverb 5.9 %, 30 releasing 6.9 %, 64 8.8 %; @128 worst 13.9 %. Peak traced allocation per render 10 KB, net churn ~35 B — no leak, no I/O, no lock across the reverb.
- `note_on` 4.7 µs (9.6 at the voice cap); `note_off`/CC 0.3–0.5 µs.
- **60 Hz panel:** `draw_plan()` 37.7 µs = 0.21 % of a core; `refresh()` no-ops in 0.11 µs so idle frames skip repaint, and the timer stops with the panel (`tray.py:180,573`).
- **Voicing switch frees the old bank:** RSS 76.1 → 31.5 MiB before the new one loads (`tray.py:347-353`).
- `_tick`: `recorder.stats()` 0.72 µs. The watcher rebuilds a `MidiIn` per poll (`midi_in.py:65-69`), 0.077 ms/1.5 s on Linux/ALSA = <0.01 % of a core; macOS CoreMIDI is an **estimate**, still ignorable.

## Dead weight (no features touched)

`keyboard.py:317` `NoteLights.__len__` — referenced nowhere. `tray.py:208` `_on_bank_ready` is `pass`; the whole `on_done` chain (`samplebank.py:141,179`) exists only for it. `samplebank.py:216-218` + `tone.py:498-499` keep a legacy call shape alive only for `tools/selftest`'s spy. Used only by `tools/`: `keyboard.py:309` `is_down`, `:137` `center_x`, `synth.py:185-187` `active_voices`, `KeyRect.right/.bottom`. Duplicated: `NOTE_MIN/MAX` (`tone.py:57-58` vs `keyboard.py:37-38`), MIDI status bytes (`tray.py:35` vs `recorder.py:25-27`), ring-buffer read/write (`reverb.py:44-66` vs `78-95`), float→int16 clip (`samplebank.py:195` vs `recorder.py:257`). No unused imports.

---
All experiments ran on a `/tmp` copy: the original repo is byte-for-byte unmodified — all 41 files md5-identical, `git status` shows nothing new.
