"""
Headless self-test for the Just Piano engine.

Exercises the sample bank, the realtime mixer, pedal handling, the recorder and
both export paths without needing any audio hardware or a MIDI keyboard.

    python3 -m tools.selftest
"""

from __future__ import annotations

import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile

# Keep the suite away from a real installation's settings/recordings, and force
# it: with `setdefault` a pre-set (or reused) JUSTPIANO_HOME would let the disk
# cache checks below inspect the *previous* run's artefacts. Must stay above the
# first `justpiano` import -- `config` reads the variable at import time.
_HOME = _tempfile.mkdtemp(prefix="justpiano-home-")
_os.environ["JUSTPIANO_HOME"] = _HOME
_atexit.register(_shutil.rmtree, _HOME, ignore_errors=True)

import hashlib
import json
import math
import mmap
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from justpiano import recorder as rec
from justpiano import samplebank
from justpiano import synth
from justpiano import tone
from justpiano.reverb import (
    CHUNK, LONGEST_LINE, PRESETS as REVERB_PRESETS, Reverb, TAIL_FLOOR,
    _ALLPASS_TUNING, _COMB_TUNING,
)
from justpiano.samplebank import TONE_FINGERPRINT, SampleBank
from justpiano.synth import HARD_MAX_VOICES, MAX_VOICES, AudioEngine

NOTE_ON, NOTE_OFF, CC = 0x90, 0x80, 0xB0

#: The suite builds far more banks than a user ever holds at once -- five
#: instruments across four sample rates, plus the planted stems the prune checks
#: need -- so the disk-cache budget is lifted out of the way for everything
#: except the one test that is about it (`test_footprint` puts it back around
#: its own eviction checks). Left at its shipped value, `prune_cache()` would
#: evict a bank another test is about to reload and read as a cache bug.
samplebank.MAX_CACHED_BANKS = 64

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))


def rms(block) -> float:
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


class render_spy:
    """Context manager counting calls into `tone.render_note`.

    A cache "hit" that quietly re-renders all 176 buffers is indistinguishable
    from a real one by timing alone on a fast machine, so the cache checks below
    assert on the number of notes that were actually synthesised.

    The call is forwarded verbatim: `render_note` grew a `voicing` argument, so a
    spy nailed to the pre-voicing `(note, layer, samplerate=...)` shape raised
    TypeError -- and took the whole suite with it -- the moment a bank rendered a
    non-default voicing with four arguments.
    """

    def __init__(self) -> None:
        self.count = 0
        self._orig = None

    def __enter__(self) -> "render_spy":
        self._orig = tone.render_note

        def spy(*a, **kw):
            self.count += 1
            return self._orig(*a, **kw)

        tone.render_note = spy
        return self

    def __exit__(self, *_exc) -> bool:
        tone.render_note = self._orig
        return False


# --------------------------------------------------------------------- bank
def test_bank() -> SampleBank:
    print("\n[1] sample bank")
    t0 = time.time()
    bank = SampleBank()
    bank.build_blocking()
    elapsed = time.time() - t0

    missing = [n for n in range(tone.NOTE_MIN, tone.NOTE_MAX + 1)
               if bank.get_pair(n) is None]
    check("all 88 keys rendered", not missing, f"missing={missing[:5]}")
    check("bank build time reasonable", elapsed < 60, f"{elapsed:.2f}s")

    total_mb = sum(b.nbytes for b in bank._data.values()) / 1e6
    check("memory footprint sane", total_mb < 120, f"{total_mb:.0f} MB")

    a4 = bank.get(69, "hard").astype(np.float32) / 32768.0
    spectrum = np.abs(np.fft.rfft(a4[:8192] * np.hanning(8192)))
    freqs = np.fft.rfftfreq(8192, 1 / 44100)
    peak_hz = float(freqs[np.argmax(spectrum)])
    check("A4 fundamental is 440 Hz", abs(peak_hz - 440.0) < 6.0, f"{peak_hz:.1f} Hz")

    soft = bank.get(60, "soft").astype(np.float32)
    hard = bank.get(60, "hard").astype(np.float32)
    def centroid(x):
        sp = np.abs(np.fft.rfft(x[:16384] * np.hanning(16384)))
        f = np.fft.rfftfreq(16384, 1 / 44100)
        return float((sp * f).sum() / sp.sum())
    check("hard layer is brighter than soft layer",
          centroid(hard) > centroid(soft) * 1.3,
          f"{centroid(soft):.0f} Hz vs {centroid(hard):.0f} Hz")

    # Tone model revision "2" cuts the partial series at a *frequency*
    # (nyquist * 0.92), not at a fixed partial index: with the old 48-partial cap
    # A0's top partial was 1.4 kHz, so the bottom octave came out audibly duller
    # than the rest of the keyboard. Measured as the strength of the sharpest
    # spectral line in 2.5-5 kHz against the median of that band, sampled past
    # the broadband hammer-noise transient so only real partials can show up.
    def hf_line(note: int, layer: str, lo=2500.0, hi=5000.0) -> float:
        buf = bank.get(note, layer).astype(np.float32) / 32768.0
        seg = buf[int(0.06 * 44100):int(0.06 * 44100) + 16384]
        sp = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        f = np.fft.rfftfreq(seg.size, 1 / 44100)
        band = sp[(f >= lo) & (f <= hi)]
        return float(band.max() / max(float(np.median(band)), 1e-12))

    a0_soft, a0_hard = hf_line(21, "soft"), hf_line(21, "hard")
    check("the bottom octave carries real high-frequency partials",
          a0_soft > 50.0 and a0_hard > 50.0,
          f"A0 line/floor: soft {a0_soft:.0f}x, hard {a0_hard:.0f}x")
    # _MAX_PARTIALS is a runaway guard, not the actual limit: if it ever became
    # the limit the cut would silently be by index again.
    f0, B = tone.midi_to_freq(21), tone.inharmonicity(21)
    needed = 1
    while needed * f0 * np.sqrt(1.0 + B * needed * needed) <= 22050.0 * 0.92:
        needed += 1
    check("the partial-count guard leaves the lowest note's series intact",
          needed < tone._MAX_PARTIALS, f"A0 needs {needed} of {tone._MAX_PARTIALS}")
    # ...and the series really is cut where the docstring says, at 0.92 of
    # Nyquist. Nothing asserted the *position* of that cut: dropping it to 0.50
    # (11 kHz) took A0 from 345 partials to 241 and left every check above green,
    # because they all look at 2.5-5 kHz, which a half-Nyquist series still
    # reaches. Both halves are asserted -- the series itself, exactly, and the
    # rendered buffer, where the top partials have to survive as real spectral
    # lines rather than as the hammer noise that also lives up there.
    idx, freqs, _phases = tone.partial_series(21, tone.DEFAULT_VOICING, 44100)
    top = float(freqs[-1])
    check("A0's partial series is cut at 0.92 of Nyquist, not at a fraction of it",
          idx.size == needed - 1 and 0.90 <= top / 22050.0 <= 0.92,
          f"{idx.size} partials (expected {needed - 1}), top {top:.0f} Hz "
          f"= {top / 22050.0:.3f} of Nyquist")
    # A0's own partials above 11 kHz are below the int16 noise floor of its own
    # buffer, so the audible half of this is asserted where the cut is loudest:
    # C6's series reaches 20 kHz, and with the cut at half Nyquist it stops at
    # 11 kHz and 14-19 kHz becomes nothing but hammer noise (measured 95x line to
    # floor against 4.5x).
    c6_top = hf_line(84, "hard", 14000.0, 19000.0)
    check("...and the top octaves of the keyboard reach 19 kHz as partials",
          c6_top > 30.0, f"C6 line/floor at 14-19 kHz: {c6_top:.1f}x")

    # ---- the int16 conversion, shared by the cache and every WAV export ----
    # `np.clip` was doing no work in any existing check, because nothing ever
    # handed to_pcm16 a sample outside [-1, 1] -- and a dense chord through the
    # soft-clipper reaches 0.92, so the margin is thin. Without the clip the
    # overshoot *wraps*: +2.0 comes out as -2 and a loud passage inverts, in the
    # cached bank and in every exported WAV.
    over = samplebank.to_pcm16(np.array([2.0, 1.0, 0.5, -0.5, -1.0, -2.0],
                                        dtype=np.float32))
    ramp = samplebank.to_pcm16(np.linspace(0.9, 2.0, 64, dtype=np.float32))
    check("to_pcm16 clips the overshoot instead of wrapping it",
          list(over) == [32767, 32767, 16384, -16384, -32767, -32768]
          and bool(np.all(np.diff(ramp) >= 0)) and int(ramp.max()) == 32767,
          f"{list(over)}, ramp {int(ramp.min())}..{int(ramp.max())}")
    # Rounded, not truncated (`astype` cuts towards zero): the whole reason
    # BANK_VERSION had to move, since the bytes changed without `tone` changing.
    lsb = 1.0 / 32767.0
    check("to_pcm16 rounds to the nearest LSB instead of truncating",
          list(samplebank.to_pcm16(np.array(
              [0.99999, 0.6 * lsb, -0.6 * lsb, 1.4 * lsb], dtype=np.float64)))
          == [32767, 1, -1, 1],
          str(list(samplebank.to_pcm16(np.array(
              [0.99999, 0.6 * lsb, -0.6 * lsb, 1.4 * lsb], dtype=np.float64)))))
    check("the cache version moved past the truncating blobs it can no longer use",
          samplebank.BANK_VERSION >= 4
          and f"_v{samplebank.BANK_VERSION}_" in f"_{bank._stem}"
          and not samplebank._stem_is_current(
              f"{samplebank.CACHE_PREFIX}3_{bank.voicing}_{bank.samplerate}"),
          f"BANK_VERSION={samplebank.BANK_VERSION}, stem={bank._stem}")
    # Per-partial envelope truncation (_ENV_TRUNC) must only drop what is already
    # inaudible: too small a value silently amputates the bass aftersound (at
    # 2.0 the A0 buffer goes dead silent from 2.9 s of its 8 s onwards).
    a0 = bank.get(21, "hard").astype(np.float32) / 32768.0
    def seg_rms(t0, t1):
        return rms(a0[int(t0 * 44100):int(t1 * 44100)])
    check("the bass aftersound rings on to the end of the buffer",
          seg_rms(3.0, 3.5) > 5e-3 and seg_rms(6.0, 6.5) > 1e-3,
          f"rms@3s={seg_rms(3.0, 3.5):.4f}, rms@6s={seg_rms(6.0, 6.5):.4f}")

    # A second construction must hit the disk cache: the artefacts have to be
    # on disk (build_blocking() swallows _save_cache() failures) and the reload
    # must not re-render a single note.
    check("disk cache artefacts are written",
          os.path.exists(bank._blob_path) and os.path.exists(bank._index_path),
          os.path.basename(bank._blob_path))

    expected_renders = (tone.NOTE_MAX - tone.NOTE_MIN + 1) * len(tone.LAYERS)
    with render_spy() as spy:
        t0 = time.time()
        cached = SampleBank()
        cached.build_blocking()
        reload_time = time.time() - t0
    check("reload is served from the cache, not re-rendered",
          spy.count == 0 and cached.ready and cached.get_pair(69) is not None,
          f"{spy.count} notes re-rendered, {reload_time:.2f}s vs {elapsed:.2f}s cold")
    check("cached reload is fast", reload_time < max(1.0, elapsed * 0.25),
          f"{reload_time:.2f}s")
    check("cached samples are identical to the rendered ones",
          np.array_equal(cached.get(69, "hard"), bank.get(69, "hard"))
          and np.array_equal(cached.get(21, "soft"), bank.get(21, "soft")))

    # A cache written by a *different* tone model must be rejected and rebuilt,
    # never silently reused (the fingerprint covers everything `tone` decides).
    had_index = os.path.exists(bank._index_path)
    if had_index:
        with open(bank._index_path) as fh:
            index = json.load(fh)
        index["tone"] = "0" * 16
        with open(bank._index_path, "w") as fh:
            json.dump(index, fh)
    with render_spy() as spy:
        stale = SampleBank()
        stale.build_blocking()
    rewritten = {}
    if os.path.exists(bank._index_path):
        with open(bank._index_path) as fh:
            rewritten = json.load(fh)
    check("a cache from another tone model is rejected and re-rendered",
          had_index and spy.count == expected_renders
          and stale.get_pair(69) is not None,
          f"{spy.count}/{expected_renders} notes re-rendered")
    check("the rebuilt cache records the current tone fingerprint",
          rewritten.get("tone") == TONE_FINGERPRINT,
          f'{rewritten.get("tone")} vs {TONE_FINGERPRINT}')

    # _save_cache() replaces the blob and the index in two separate os.replace()
    # calls, so a crash in between leaves a fresh blob addressed by a stale
    # index. numpy slicing never raises on out-of-range bounds, so without the
    # explicit guards such a pair loads as silently truncated buffers. Asserted
    # against _load_cache() directly: no extra render pass is needed.
    with open(bank._index_path) as fh:
        good_index = json.load(fh)

    def with_index(mutate) -> bool:
        broken = json.loads(json.dumps(good_index))
        mutate(broken)
        with open(bank._index_path, "w") as fh:
            json.dump(broken, fh)
        try:
            return SampleBank()._load_cache()
        finally:
            with open(bank._index_path, "w") as fh:
                json.dump(good_index, fh)

    def stale_start(index):
        # A fresh blob addressed by a stale index: the length is still right for
        # the note, only the offset points past the end of the buffer.
        index["offsets"]["60:hard"][0] += 10 ** 9

    def negative_start(index):
        index["offsets"]["60:hard"][0] = -1

    def wrong_length(index):
        index["offsets"]["60:hard"][1] -= 100

    def unknown_note(index):
        # Same length as the entry it replaces (note_duration clamps at NOTE_MAX),
        # so only the note-range guard can reject this one.
        index["offsets"]["999:hard"] = index["offsets"].pop("108:hard")

    def unknown_layer(index):
        # Same note, so the length still matches: only the layer guard applies.
        index["offsets"]["60:thunder"] = index["offsets"].pop("60:soft")

    check("an index that overruns the blob is rejected, not loaded short",
          with_index(stale_start) is False)
    check("a negative offset is rejected", with_index(negative_start) is False)
    check("a buffer of the wrong length is rejected, not stretched",
          with_index(wrong_length) is False)
    check("an unknown note number in the index is rejected",
          with_index(unknown_note) is False)
    check("an unknown velocity layer in the index is rejected",
          with_index(unknown_layer) is False)
    check("the restored index still loads", SampleBank()._load_cache() is True)

    # The fingerprint is what invalidates the cache, and in the shipped .app
    # `inspect.getsource(tone)` *always* fails -- so every signal it folds in
    # has to work without the source. Each case below re-derives the digest with
    # getsource() forced to fail.
    import inspect as _inspect

    def frozen_fingerprint():
        real = _inspect.getsource

        def no_source(*_a, **_kw):
            raise OSError("source not available")   # what the .app really does

        _inspect.getsource = no_source
        try:
            return samplebank._tone_fingerprint()
        finally:
            _inspect.getsource = real

    frozen_base = frozen_fingerprint()

    def has_nested_code():
        return [x * 2 for x in range(3)]    # a comprehension = a nested code object

    check("code signatures are address-free, nested code objects included",
          "0x" not in repr(samplebank._code_signature(has_nested_code.__code__)),
          repr(samplebank._code_signature(has_nested_code.__code__))[:70])
    # The real proof that the digest holds no addresses: a *separate* process
    # must arrive at the same value, or the shipped app would rebuild all 176
    # buffers on every single launch.
    other = subprocess.run(
        [sys.executable, "-c",
         "from justpiano.samplebank import TONE_FINGERPRINT; print(TONE_FINGERPRINT)"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True)
    check("a fresh process computes the same fingerprint",
          other.stdout.strip() == TONE_FINGERPRINT,
          f"{other.stdout.strip()!r} vs {TONE_FINGERPRINT!r}")
    check("the fingerprint is stable across calls", frozen_base == frozen_fingerprint(),
          frozen_base)

    def fingerprint_after(name, value):
        """Digest with `tone.<name>` temporarily replaced, source unreadable."""
        original = getattr(tone, name)
        setattr(tone, name, value)
        try:
            return frozen_fingerprint()
        finally:
            setattr(tone, name, original)

    def other_note_duration(note):    # a different tone model, same signature
        return 4.0

    check("a bumped TONE_MODEL_REVISION invalidates the cache in a frozen app",
          fingerprint_after("TONE_MODEL_REVISION", "999") != frozen_base)
    check("a changed module constant invalidates the cache in a frozen app",
          fingerprint_after("_ENV_TRUNC", 9.0) != frozen_base)
    check("changed function code invalidates the cache in a frozen app",
          fingerprint_after("note_duration", other_note_duration) != frozen_base)

    # A render failure used to kill the bank-build thread outright: `ready`
    # stayed False, `on_done` never fired and the tray sat on a frozen
    # "Building piano… N%" for the rest of the session.
    orig_render = tone.render_note

    def explode(*_a, **_kw):
        raise MemoryError("cannot allocate 2.1 GiB")

    def join_build(bank, timeout=30.0):
        deadline = time.time() + timeout
        while bank._thread is not None and bank._thread.is_alive() \
                and time.time() < deadline:
            time.sleep(0.02)

    quiet, threading.excepthook = threading.excepthook, lambda args: None
    try:
        boom = SampleBank(samplerate=22050)   # no cache for that rate -> builds
        done = []
        tone.render_note = explode
        try:
            boom.load_or_build_async(on_done=lambda: done.append(True))
            join_build(boom)
        finally:
            tone.render_note = orig_render
        check("a failed build records the error instead of dying silently",
              boom.error is not None and "allocate" in boom.error and not boom.ready,
              f"error={boom.error!r} ready={boom.ready}")
        check("on_done still fires after a failed build", done == [True], str(done))

        # A raising callback must not be mistaken for a corrupt cache: that
        # silently re-rendered all 176 buffers and called on_done twice.
        calls = []

        def angry_done():
            calls.append(True)
            raise RuntimeError("callback blew up")

        with render_spy() as spy:
            served = SampleBank()
            served.load_or_build_async(on_done=angry_done)
            join_build(served)
        check("a raising callback is not mistaken for a corrupt cache",
              spy.count == 0 and calls == [True] and served.ready,
              f"{spy.count} notes re-rendered, {len(calls)} callback(s)")
    finally:
        threading.excepthook = quiet
    return bank


# ------------------------------------------------------------------- engine
def test_engine(bank: SampleBank) -> None:
    print("\n[2] realtime engine")
    engine = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off")

    silence = engine.render(256)
    check("silent when idle", rms(silence) == 0.0)

    engine.note_on(60, 100)
    block = engine.render(256)
    check("note_on produces audio", rms(block) > 1e-4, f"rms={rms(block):.4f}")
    # `<= 1.0` would be a tautology: render() ends in tanh()*0.92, so the block
    # is bounded by 0.92 whatever the mix does. Assert the band a single
    # mezzoforte note must sit in instead -- audible, and clear of the limiter.
    peak = float(np.abs(block).max())
    check("a single note is audible and stays clear of the limiter",
          np.isfinite(block).all() and 0.05 < peak < 0.80, f"peak={peak:.3f}")
    check("stereo panning is applied",
          not np.allclose(block[:, 0], block[:, 1]))

    # ---- keys this piano does not have ------------------------------------
    # Rejected, not clamped. Folding note 19/20 onto A0 invented a note the
    # player never played (a 5-octave controller transposed down two octaves
    # sprays them), and -- worse -- the matching note_off(19) then released a
    # genuinely held A0 under the player's fingers.
    edges = AudioEngine(bank, volume=1.0, reverb_preset="off")
    edges.note_on(21, 110)                 # a real A0, held down
    edges.render(1024)
    for stray in (19, 20, 109, 127, -1):
        edges.note_on(stray, 110)
    check("a note below A0 or above C8 is refused, not folded onto a real key",
          edges.active_voices == 1 and sorted(edges._by_note) == [21],
          f"{edges.active_voices} voices for {sorted(edges._by_note)}")
    for stray in (19, 20, 109, 127, -1):
        edges.note_off(stray)
    edges.render(1024)
    held_on = rms(edges.render(1024))
    check("...and a note_off for one of them cannot release a held A0",
          edges.active_voices == 1 and held_on > 5e-3
          and not edges._by_note[21][0].releasing,
          f"{edges.active_voices} voices, rms={held_on:.5f}")
    # The bank has no buffer for them either, so a clamp would also have been a
    # silent lie about which sample was playing.
    check("the sample bank agrees the keyboard stops at 21..108",
          bank.get_pair(20) is None and bank.get_pair(109) is None
          and (tone.NOTE_MIN, tone.NOTE_MAX) == (21, 108),
          f"{tone.NOTE_MIN}..{tone.NOTE_MAX}")

    # render(0): PortAudio has been seen to ask for nothing, and the WAV
    # exporter's block loop can end on it. It used to raise IndexError off
    # `env[-1]` of an empty mute envelope, which `_callback` turned into a block
    # of silence -- and the mute ramp must not be advanced by a zero-length step.
    zero = AudioEngine(bank, volume=1.0, reverb_preset="hall")
    zero.note_on(60, 100)
    zero.render(256)
    zero.set_muted(True)
    ramping = zero.render(256)              # the ramp is now part-way down
    mid_ramp = zero.mute_gain
    empty = _safe(zero.render, 0)
    check("render(0) is an empty block, not an exception",
          isinstance(empty, np.ndarray) and empty.shape == (0, 2)
          and empty.dtype == np.float32 and zero.mute_gain == mid_ramp
          and _raises(zero.render, 0) is None,
          f"{None if empty is None else empty.shape}, "
          f"gain {mid_ramp:.4f} -> {zero.mute_gain:.4f}")
    resumed = zero.render(256)
    check("...and it does not cost the mute ramp a step or leave a seam",
          0.0 <= zero.mute_gain < mid_ramp
          and edge_step(ramping, resumed) <= max_step(ramping) * 1.5
          and np.isfinite(resumed).all(),
          f"gain {mid_ramp:.4f} -> {zero.mute_gain:.4f}, seam "
          f"{edge_step(ramping, resumed):.5f} vs {max_step(ramping):.5f} in-block")

    loud = AudioEngine(bank, volume=1.0, reverb_preset="off")
    quiet = AudioEngine(bank, volume=1.0, reverb_preset="off")
    loud.note_on(60, 127)
    quiet.note_on(60, 30)
    check("velocity changes loudness",
          rms(loud.render(4096)) > rms(quiet.render(4096)) * 2.0)

    # release
    engine.note_off(60)
    engine.render(256)
    before = rms(engine.render(1024))
    for _ in range(20):
        engine.render(1024)
    after = rms(engine.render(1024))
    # 0.25 was not a bound at all: ~0.49 s of the deep-decaying sample buffer
    # drops that far on its own, so a `release()` that did nothing still passed.
    # A working damper (RELEASE_TAU = 0.16) lands at ~0.010, a dead one at
    # ~0.214, so 0.05 separates them with a 5x margin on either side.
    check("note_off damps the string", after < before * 0.05,
          f"{before:.4f} -> {after:.6f}")

    # Sustain pedal. Deliberately on a bass key: note 60's buffer is only 2.7 s
    # long, so the old 151 blocks (3.5 s) outlived the sample and *both* pedal
    # checks passed even with CC64 handling ripped out. Note 28 rings for 6.6 s,
    # far past the 2.6 s rendered here, so a voice that stops can only have been
    # released.
    ped = AudioEngine(bank, volume=1.0, reverb_preset="off")
    ped.control_change(64, 127)
    ped.note_on(28, 100)
    ped.render(1024)
    ped.note_off(28)
    for _ in range(30):           # 0.7 s: an un-pedalled release is long over
        ped.render(1024)
    ringing = rms(ped.render(1024))
    check("sustain pedal keeps the note ringing",
          ringing > 5e-3 and ped.active_voices == 1,
          f"rms={ringing:.5f}, {ped.active_voices} voices")
    ped.control_change(64, 0)
    for _ in range(80):           # release tau is 160 ms -> give it ~1.9 s
        ped.render(1024)
    damped = rms(ped.render(1024))
    check("pedal release damps the note",
          damped < 1e-5 and ped.active_voices == 0,
          f"rms={damped:.2e}, {ped.active_voices} voices left")

    # ---- the rest of control_change(): CC66, CC67, CC120, CC121, CC123 ----
    # Every one of these branches used to be dead code as far as both suites
    # were concerned. All of them use bass keys for the same reason as above.
    def bass_engine():
        # `strike_variation=0` on purpose. Nothing below is testing the strike
        # variation, and two of these checks read a *single sample* of a chord
        # that has been ringing for 1.4 s -- `hard_seam`, the amplitude a hard
        # cut would have stepped from. Detune the four notes against each other
        # by a cent and where their sum lands on that one sample is arbitrary:
        # the measurement drifts between 0.011 and 0.09 with nothing about the
        # declick having changed. Strike variation has its own section ([12]).
        # `resonance=0` for the same kind of reason as `strike_variation=0`, but
        # a sharper one: `immediate_cut` measures the release envelope as a
        # per-sample ratio against an un-cut twin, and that method needs the
        # signal path to be a replay. The sympathetic bank is a feedback system
        # *driven by the mix*, so after a cut its output is a delayed echo of a
        # signal that is no longer there while the twin's is still being fed --
        # the ratio stops being an envelope and the monotonicity clause fails on
        # a system that is behaving correctly. The cut is checked with the bank
        # running in [12] instead, where it is measured on the samples rather
        # than on a ratio.
        return AudioEngine(bank, volume=1.0, reverb_preset="off",
                           strike_variation=0.0, resonance=0.0)

    # Sostenuto (CC66) latches exactly the notes that were down when the pedal
    # went down -- and a repeating pedal that resends CC66 while it is held must
    # not retroactively latch notes played after the press.
    sos = bass_engine()
    sos.note_on(28, 100)          # held at the press -> latched
    sos.render(1024)
    sos.control_change(66, 127)
    check("CC66 down latches the engine's sostenuto state", sos.sostenuto_down)
    sos.note_on(33, 100)          # played after the press -> NOT latched
    sos.render(1024)
    sos.control_change(66, 127)   # a continuous pedal resends this while held
    sos.note_off(28)
    sos.note_off(33)
    for _ in range(70):           # 1.6 s: long enough for a 160 ms release to
        sos.render(1024)          # retire the un-latched voice completely
    held = sorted({note for note, voices in sos._by_note.items() if voices})
    sos_rms = rms(sos.render(1024))
    check("sostenuto holds only the notes that were down at the pedal edge",
          held == [28] and sos_rms > 5e-3 and sos.active_voices == 1,
          f"held={held} rms={sos_rms:.5f}")
    sos.control_change(66, 0)
    for _ in range(80):
        sos.render(1024)
    check("CC66 up releases the latched note and clears the latch",
          sos.active_voices == 0 and not sos.sostenuto_down,
          f"{sos.active_voices} voices, down={sos.sostenuto_down}")

    # Soft pedal (CC67): una corda scales both velocity and the layer mix down.
    soft_down, soft_up = bass_engine(), bass_engine()
    soft_down.control_change(67, 127)
    check("CC67 down engages the soft pedal", soft_down.soft_pedal)
    soft_down.note_on(72, 100)
    soft_up.note_on(72, 100)
    loud_rms, soft_rms = rms(soft_up.render(8192)), rms(soft_down.render(8192))
    check("soft pedal makes the same note quieter", soft_rms < loud_rms * 0.9,
          f"{soft_rms:.5f} vs {loud_rms:.5f}")
    soft_down.control_change(67, 0)
    check("CC67 up lifts the soft pedal", not soft_down.soft_pedal)

    # CC120 (All Sound Off) is immediate silence, and it deliberately leaves the
    # pedal latches alone: the damper is still physically down, so inverting it
    # here would mean the next note stops ringing under a held pedal.
    #
    # "Immediate" is a ~1.2 ms declick release (DECLICK_TAU), *not* a dropped
    # voice list. This check used to assert `rms(next block) == 0.0`, which is
    # what the defect did: it left a measured 0.343 step at the block boundary
    # (0.92 at volume 1.5 -- a full-scale edge), i.e. an audible click on every
    # All Sound Off. So the assertion is no longer "the next block is silent" but
    # the intent that was meant all along -- nothing is left for a key or a pedal
    # to reach, the audio already in flight fades out monotonically with no step,
    # and only *then* is there exact silence.
    def sound_off_engine():
        eng = bass_engine()
        eng.control_change(64, 127)
        for note in (28, 33, 40, 47):
            eng.note_on(note, 100)
        return eng

    sound_off = immediate_cut(sound_off_engine, lambda e: e.control_change(120, 0))
    check("all-sound-off precondition: four voices are sounding",
          sound_off.voices_before == 4, f"{sound_off.voices_before} voices")
    cut_ok, cut_detail = declicked(sound_off)
    check("CC120 silences everything at once and keeps the damper latched",
          cut_ok and sound_off.engine.sustain,
          f"{cut_detail}, sustain={sound_off.engine.sustain}")

    # CC123 (All Notes Off) is "every key up", nothing more: a held damper keeps
    # the notes ringing until the pedal itself is lifted.
    notes_off = bass_engine()
    notes_off.control_change(64, 127)
    for note in (28, 33):
        notes_off.note_on(note, 100)
    notes_off.render(1024)
    notes_off.control_change(123, 0)
    for _ in range(30):
        notes_off.render(1024)
    still_ringing = rms(notes_off.render(1024))
    check("CC123 lifts the keys but the held damper keeps the notes ringing",
          notes_off.active_voices == 2 and still_ringing > 5e-3
          and notes_off.sustain,
          f"{notes_off.active_voices} voices, rms={still_ringing:.5f}, "
          f"sustain={notes_off.sustain}")
    notes_off.control_change(64, 0)   # only now may they be damped
    for _ in range(80):
        notes_off.render(1024)
    check("lifting the damper after CC123 releases the keys it let go",
          notes_off.active_voices == 0, f"{notes_off.active_voices} voices")

    # CC121 (Reset All Controllers) is the *only* CC that clears the latches,
    # and it must not cut the notes that are still being held down.
    reset_cc = bass_engine()
    reset_cc.control_change(64, 127)
    reset_cc.control_change(67, 127)
    reset_cc.note_on(28, 100)
    reset_cc.render(1024)
    reset_cc.control_change(66, 127)
    check("reset-controllers precondition: all three pedals are down",
          reset_cc.sustain and reset_cc.soft_pedal and reset_cc.sostenuto_down)
    reset_cc.control_change(121, 0)
    reset_rms = rms(reset_cc.render(1024))
    check("CC121 puts every pedal back up without cutting the held note",
          not reset_cc.sustain and not reset_cc.soft_pedal
          and not reset_cc.sostenuto_down and reset_cc.active_voices == 1
          and reset_rms > 5e-3,
          f"sustain={reset_cc.sustain} soft={reset_cc.soft_pedal} "
          f"sostenuto={reset_cc.sostenuto_down} rms={reset_rms:.5f}")

    # all_notes_off() is the tray's Panic and restart(): unlike CC123 it is a
    # full panic that drops the pedal latches too, immediate or not.
    def panic_engine():
        eng = bass_engine()
        eng.control_change(64, 127)
        eng.control_change(67, 127)
        eng.note_on(28, 100)
        eng.note_on(33, 100)
        eng.note_on(40, 100)
        eng.note_on(47, 100)
        eng.render(1024)
        eng.control_change(66, 127)
        return eng

    # The immediate panic is the click-free one: same rewrite as CC120 above --
    # zero voices at once, then a monotonic DECLICK_TAU fade with no step over
    # 0.01, then exact silence -- plus the pedal latches it is supposed to drop.
    # 20 blocks of ring-out (0.46 s), because the soft pedal takes the chord down
    # far enough that after 1.4 s a hard cut would no longer be loud enough to
    # click, and `declicked()` refuses to draw a conclusion from that.
    panic = immediate_cut(panic_engine,
                          lambda e: e.all_notes_off(immediate=True), warm=20)
    pedals_up = (not panic.engine.sustain and not panic.engine.soft_pedal
                 and not panic.engine.sostenuto_down)
    cut_ok, cut_detail = declicked(panic)
    check("all_notes_off(immediate=True) silences and lifts every pedal",
          cut_ok and pedals_up,
          f"{cut_detail}, sustain={panic.engine.sustain}, "
          f"soft={panic.engine.soft_pedal}, "
          f"sostenuto={panic.engine.sostenuto_down}")

    # The soft panic keeps the old shape: it is allowed to take its 80 ms.
    slow = panic_engine()
    slow.all_notes_off(immediate=False)
    for _ in range(30):
        slow.render(1024)
    check("all_notes_off(immediate=False) silences and lifts every pedal",
          slow.active_voices == 0 and rms(slow.render(512)) == 0.0
          and not slow.sustain and not slow.soft_pedal
          and not slow.sostenuto_down,
          f"{slow.active_voices} voices, sustain={slow.sustain}, "
          f"soft={slow.soft_pedal}, sostenuto={slow.sostenuto_down}")

    # polyphony limit + cleanup. The burst is sized off the constant under test:
    # with fewer note_ons than HARD_MAX_VOICES the assertion would hold no
    # matter what _enforce_polyphony() does.
    poly = AudioEngine(bank, reverb_preset="off")
    for i in range(HARD_MAX_VOICES + 40):
        poly.note_on(21 + i % 88, 90)
    check("burst of note_ons stays under the hard cap",
          poly.active_voices <= HARD_MAX_VOICES,
          f"{poly.active_voices} voices for {HARD_MAX_VOICES + 40} note_ons")
    for _ in range(24):           # ~0.55 s: stolen voices fade out and retire
        poly.render(1024)
    check("polyphony settles back to the soft limit",
          poly.active_voices <= MAX_VOICES, f"{poly.active_voices} voices")

    flood = AudioEngine(bank, reverb_preset="off")
    for i in range(2000):         # stuck controller spraying note_on
        flood.note_on(21 + i % 88, 90)
    check("pathological input cannot grow the mix list",
          flood.active_voices <= HARD_MAX_VOICES, f"{flood.active_voices} voices")

    # re-striking the same key must not accumulate voices
    retrig = AudioEngine(bank, reverb_preset="off")
    for _ in range(40):           # 10 strikes/second on one key
        retrig.note_on(64, 100)
        retrig.render(4410)
    check("re-strike does not leak voices", retrig.active_voices <= 5,
          f"{retrig.active_voices} voices")

    # panic, on top of the re-strike storm above: the voices in fast release from
    # the stolen strikes and the one key still held all have to go at once. This
    # check also used to assert `rms(next block) == 0.0` straight after the cut,
    # i.e. the click; it now asserts the declick (see CC120 above) and keeps the
    # exact silence one block later, where it belongs.
    def restruck_engine():
        eng = AudioEngine(bank, reverb_preset="off", volume=1.0)
        for _ in range(40):       # 10 strikes/second on one key
            eng.note_on(40, 100)
            eng.render(4410)
        return eng

    storm = immediate_cut(restruck_engine,
                          lambda e: e.all_notes_off(immediate=True), warm=20)
    cut_ok, cut_detail = declicked(storm)
    check("panic silences everything", cut_ok, cut_detail)

    # voices free themselves once the sample ends
    finish = AudioEngine(bank, reverb_preset="off")
    finish.note_on(105, 100)
    for _ in range(int(44100 * 2 / 1024)):
        finish.render(1024)
    check("finished voices are reclaimed", finish.active_voices == 0,
          f"{finish.active_voices} left")

    # reverb energy decays to nothing
    verb = AudioEngine(bank, volume=1.0, reverb_preset="hall")
    for n in (36, 48, 60, 72):
        verb.note_on(n, 127)
    for _ in range(int(44100 * 20 / 1024)):
        last = verb.render(1024)
    check("reverb tail is stable and decays", rms(last) < 1e-6 and np.isfinite(last).all(),
          f"rms={rms(last):.2e}")

    # A dense chord must be *compressed* by the static soft-clipper. `peak <=
    # 1.0` proves nothing (render() ends in tanh()*0.92), so assert what the
    # limiter does not guarantee: the loud mix has to arrive at the 0.92 ceiling
    # while a 5x quieter one stays far below it, i.e. the curve saturates.
    def chord_peak(volume):
        eng = AudioEngine(bank, volume=volume, reverb_preset="hall")
        for note in range(36, 60):
            eng.note_on(note, 127)
        return max(float(np.abs(eng.render(256)).max()) for _ in range(40))

    loud_peak = chord_peak(1.25)
    soft_peak = chord_peak(0.25)
    check("dense fortissimo chord reaches the soft-clip ceiling",
          0.90 < loud_peak <= 0.92, f"peak={loud_peak:.4f}")
    check("the soft-clipper compresses instead of scaling linearly",
          soft_peak > 0.2 and loud_peak < soft_peak * 2.0,
          f"x5 gain only raised the peak {loud_peak / max(soft_peak, 1e-9):.2f}x "
          f"({soft_peak:.3f} -> {loud_peak:.3f})")


# -------------------------------------------------------- audio thread locking
class lock_spy:
    """Counts acquisitions of one lock, per thread, and times how long it is held.

    `AudioEngine` only ever uses its locks as context managers, so `__enter__` is
    the whole of the instrumentation; `acquire`/`release` are forwarded anyway so
    that swapping this in cannot change behaviour if that stops being true.
    `worst_hold` is what the thread waiting outside would have waited.
    """

    def __init__(self, lock) -> None:
        self.lock = lock
        self._lock = lock
        self.by_thread: dict[int, int] = {}
        self.holds: list[float] = []
        self.waits: list[float] = []
        self._since: dict[int, float] = {}

    @property
    def total(self) -> int:
        return sum(self.by_thread.values())

    @property
    def worst_hold(self) -> float:
        return max(self.holds) if self.holds else 0.0

    @property
    def worst_wait(self) -> float:
        """The longest anyone waited at the door for it."""
        return max(self.waits) if self.waits else 0.0

    def taken_by(self, ident: int) -> int:
        return self.by_thread.get(ident, 0)

    def _count(self) -> None:
        ident = threading.get_ident()
        self.by_thread[ident] = self.by_thread.get(ident, 0) + 1

    def __enter__(self):
        self._count()
        asked = time.perf_counter()
        entered = self._lock.__enter__()
        now = time.perf_counter()
        self.waits.append(now - asked)
        self._since[threading.get_ident()] = now
        return entered

    def __exit__(self, *exc):
        since = self._since.pop(threading.get_ident(), None)
        if since is not None:
            self.holds.append(time.perf_counter() - since)
        return self._lock.__exit__(*exc)

    def acquire(self, *a, **kw):                 # pragma: no cover - see above
        self._count()
        got = self._lock.acquire(*a, **kw)
        self._since[threading.get_ident()] = time.perf_counter()
        return got

    def release(self):                           # pragma: no cover - see above
        since = self._since.pop(threading.get_ident(), None)
        if since is not None:
            self.holds.append(time.perf_counter() - since)
        return self._lock.release()


def test_locking(bank: SampleBank) -> None:
    """What the audio callback is allowed to wait for: nothing.

    `render()` used to take the note lock to read the voice list, so a MIDI burst
    holding it (an unfair RLock, an unthrottled event stream) convoyed the
    callback and cost whole blocks. `note_on` now builds its voice off-lock and
    splices it in through a deque, and the callback's only lock is `_evt_lock` --
    taken on the rare block that carries a menu request, never on a note.
    """
    print("\n[2b] audio thread locking")
    eng = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="hall")
    notes = lock_spy(eng._lock)
    events = lock_spy(eng._evt_lock)
    eng._lock, eng._evt_lock = notes, events

    # First: the spy really does see this lock being taken, or everything below
    # would pass on an engine that has no lock at all.
    before = notes.total
    eng.note_on(60, 100)
    eng.note_off(60)
    check("the note path takes the note lock (so the counter below can fail)",
          notes.total - before >= 2, f"{notes.total - before} acquisitions")

    during = {"notes": 0, "events": 0}

    def render(frames: int = 256):
        """One block, with the acquisitions it made attributed to the callback."""
        n0, e0 = notes.total, events.total
        block = eng.render(frames)
        during["notes"] += notes.total - n0
        during["events"] += events.total - e0
        return block

    # A block of everything the callback does: adopt spliced voices, mix, run the
    # release curves, retire voices, reap the mix list, steal past the cap, cut
    # everything immediately, fade out, clear the tank, and be asked for nothing.
    for note in (40, 47, 52, 55, 60, 64, 67, 72):
        eng.note_on(note, 100)
    eng.control_change(64, 127)
    blocks = [render() for _ in range(8)]
    eng.control_change(64, 0)
    eng.control_change(123, 0)
    blocks += [render() for _ in range(60)]
    for i in range(HARD_MAX_VOICES + 40):
        eng.note_on(21 + i % 88, 90)
    blocks += [render() for _ in range(4)]
    eng.control_change(120, 0)
    blocks += [render() for _ in range(4)]
    render(0)
    mix_only = dict(during)
    check("the callback never takes the note lock, whatever the block contains",
          mix_only["notes"] == 0,
          f"{mix_only['notes']} acquisitions over {len(blocks)} blocks")
    check("...and it does not take the control lock on an ordinary block either",
          mix_only["events"] == 0, f"{mix_only['events']} acquisitions")
    # ...but it does take `_evt_lock` exactly once on a block that carries one of
    # the rare menu requests, which is what makes read-and-clear atomic.
    eng.set_muted(True)
    eng.set_reverb("room")
    e0 = events.total
    render()
    check("a block carrying a menu request takes the control lock once, briefly",
          events.total - e0 == 1 and eng._pending_mute is None
          and eng._pending_reverb is None,
          f"{events.total - e0} acquisitions")
    eng.set_muted(False)
    for _ in range(40):
        render()

    # ---- the splice: strike order, and the cap dropping the oldest ---------
    order = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off")
    struck = [40, 47, 52, 55, 60]
    for note in struck:
        order.note_on(note, 100)
    order.render(256)
    by_voice = {id(v): n for n, voices in order._by_note.items() for v in voices}
    adopted = [by_voice.get(id(v)) for v in order._voices]
    check("voices reach the mix list in the order they were struck",
          adopted == struck, f"{adopted} vs {struck}")
    flood = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off")
    sprayed = [21 + i % 88 for i in range(HARD_MAX_VOICES + 60)]
    for note in sprayed:
        flood.note_on(note, 90)
    flood.render(256)
    by_voice = {id(v): n for n, voices in flood._by_note.items() for v in voices}
    kept = [by_voice.get(id(v)) for v in flood._voices]
    check("past the cap it is the oldest strikes that are dropped, in one slice",
          len(kept) == HARD_MAX_VOICES and kept == sprayed[-HARD_MAX_VOICES:],
          f"{len(kept)} voices, first {kept[:3]} last {kept[-3:]}")

    # ---- the callback cannot be held up, even by a lock that is held -------
    # The direct proof, and the one that does not depend on how the GIL happens
    # to be scheduled: somebody else holds the note lock for 300 ms (the shape of
    # the `_enforce_polyphony` sort that used to run under it, measured at 3.3 s
    # worst case) while the callback is asked for a block. Before the fix the
    # block waited for the lock; now it does not touch it, so it must come back
    # in a fraction of the hold -- and it must still be the right audio.
    held = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="hall")
    for note in (52, 59, 64):
        held.note_on(note, 100)
    reference = rms(held.render(256))          # adopt the voices, and a yardstick
    HOLD = 0.30
    grabbed = threading.Event()

    def hog() -> None:
        with held._lock:                       # e.g. a MIDI burst mid-registry
            grabbed.set()
            time.sleep(HOLD)

    hand = threading.Thread(target=hog, name="midi-holding-the-lock")
    hand.start()
    grabbed.wait(5.0)
    t0 = time.perf_counter()
    blocked = held.render(256)
    waited = time.perf_counter() - t0
    hand.join(10.0)
    check("a block renders straight through a 300 ms hold of the note lock",
          waited < HOLD / 10.0 and rms(blocked) > reference * 0.5,
          f"{waited * 1e3:.2f} ms of a {HOLD * 1e3:.0f} ms hold "
          f"(budget {HOLD * 100:.0f} ms), rms {rms(blocked):.4f}")

    # ---- and the same under a real MIDI flood ------------------------------
    # Four threads at once: two rtmidi callbacks (hardware + the always-on
    # virtual port) plus the menu and the hotkey are all real, and none of them
    # may be able to hold the callback up.
    live = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="hall")
    live_notes, live_events = lock_spy(live._lock), lock_spy(live._evt_lock)
    live._lock, live._evt_lock = live_notes, live_events
    stop = threading.Event()
    errors: list[BaseException] = []
    sent = [0]
    menu = [0]
    sent_lock = threading.Lock()

    def midi_flood(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for i in range(3000):
                note = 21 + rng.randrange(88)
                live.note_on(note, 40 + rng.randrange(87))
                if i % 3 == 0:
                    live.note_off(note)
                if i % 97 == 0:
                    live.control_change(rng.choice((64, 66, 67)),
                                        rng.choice((0, 127)))
                if i % 501 == 0:
                    live.set_muted(False)      # the menu item, mid-flood
                    with sent_lock:
                        menu[0] += 1
            with sent_lock:
                sent[0] += 3000
        except BaseException as exc:      # pragma: no cover - the point of this
            errors.append(exc)

    def audio_thread() -> None:
        try:
            while not stop.is_set():
                t0 = time.perf_counter()
                block = live.render(256)
                spent.append(time.perf_counter() - t0)
                if not np.isfinite(block).all():
                    errors.append(AssertionError("a block came out non-finite"))
                peaks.append(float(np.abs(block).max()))
                sizes.append(len(live._voices))
        except BaseException as exc:      # pragma: no cover - the point of this
            errors.append(exc)

    spent: list[float] = []
    peaks: list[float] = []
    sizes: list[int] = []
    callback = threading.Thread(target=audio_thread, name="fake-portaudio")
    callback.start()
    floods = [threading.Thread(target=midi_flood, args=(s,), name=f"midi-{s}")
              for s in range(4)]
    for t in floods:
        t.start()
    for t in floods:
        t.join(120.0)
    stop.set()
    callback.join(30.0)
    median = sorted(spent)[len(spent) // 2] if spent else float("nan")
    check("a 12,000-event MIDI flood never reaches the callback's note lock",
          not errors and sent[0] == 12000
          and live_notes.total >= 12000
          and live_notes.taken_by(callback.ident) == 0,
          f"{live_notes.total} note-lock acquisitions in all, "
          f"{live_notes.taken_by(callback.ident)} of them on the audio thread; "
          f"{len(spent)} blocks, median {median * 1e3:.2f} ms; errors={errors[:1]}")
    # `_evt_lock` is the one lock the callback may take, and only for the blocks
    # that actually carry a menu request: never more than one per request, and
    # nothing here is per-note.
    check("...and takes the control lock only for the menu requests themselves",
          menu[0] > 0 and live_events.taken_by(callback.ident) <= menu[0]
          and live.muted is False,
          f"{live_events.taken_by(callback.ident)} acquisitions on the audio "
          f"thread for {menu[0]} set_muted() calls, over {len(spent)} blocks")
    check("...and the mix stays bounded, finite and audible right through it",
          bool(sizes) and max(sizes) <= HARD_MAX_VOICES
          and max(peaks) > 0.01 and max(peaks) <= 0.93,
          f"{max(sizes or [0])} voices at most, peak {max(peaks or [0.0]):.3f}")
    live.all_notes_off(immediate=True)
    for _ in range(40):
        live.render(256)
    live.reverb.reset()          # the tank is still ringing; the voices are not
    check("every voice the flood created is accounted for afterwards",
          live.active_voices == 0 and not live._voices and not live._pending
          and live._by_note == {} and rms(live.render(256)) == 0.0,
          f"{live.active_voices} voices, {len(live._voices)} mixing, "
          f"{len(live._pending)} pending")


# -------------------------------------------------------------------- reverb
def test_reverb(bank: SampleBank) -> None:
    print("\n[3] reverb")
    # The block-vectorised recursion is only exact while a chunk is shorter than
    # every delay line: a longer CHUNK reads samples written by the same call,
    # which is silently wrong DSP rather than an error. reverb.py asserts this at
    # import time; assert it here too so the constant cannot drift past a line.
    shortest = min(_COMB_TUNING + _ALLPASS_TUNING)
    check("the reverb chunk fits inside every delay line", CHUNK <= shortest,
          f"CHUNK={CHUNK} vs shortest delay line {shortest}")

    # A disabled reverb returns early from process(), so its delay lines keep
    # whatever they held when it was switched off. Re-enabling must start from
    # silence rather than recirculate that ghost tail.
    verb = Reverb("hall")
    for _ in range(6):
        loud = np.full((256, 2), 0.5, dtype=np.float32)
        verb.process(loud)

    def lines(reverb) -> float:
        """Peak level still sitting in the comb delay lines."""
        return float(np.abs(np.concatenate(
            [c.buf for c in reverb._combs_l + reverb._combs_r])).max())

    check("delay lines hold energy after processing", lines(verb) > 1e-4,
          f"{lines(verb):.4f}")
    verb.set_preset("off")
    check("the off preset is silent but keeps its state",
          not verb.enabled and verb.wet == 0.0 and lines(verb) > 1e-4)
    verb.set_preset("hall")
    check("re-enabling clears the stale delay lines", lines(verb) == 0.0,
          f"{lines(verb):.2e}")
    silence = np.zeros((1024, 2), dtype=np.float32)
    verb.process(silence)
    check("no ghost tail leaks out after the disabled->enabled transition",
          rms(silence) == 0.0, f"rms={rms(silence):.2e}")
    check("presets are named back for the menu",
          verb.name == "hall" and set(REVERB_PRESETS) == {"off", "room", "hall"})

    # ---- a preset the arithmetic cannot survive ---------------------------
    # `feedback = roomsize * 0.28 + 0.70`, and `tail_frames` divides by
    # `log(feedback)`: a roomsize of 1.0715 or more asks for a comb that never
    # decays, so the log is zero and set_preset() raised ZeroDivisionError -- on
    # the audio thread, where an exception is a dead callback rather than a
    # traceback. Anything above it diverges instead, which comes out as a
    # *negative* tail bound and makes the idle gate chop a live tail.
    from justpiano import reverb as reverb_mod
    guarded = Reverb("room")
    # The ceiling as float32 sees it: `feedback` is stored as np.float32, and
    # 0.995 rounds *up* on the way in, so comparing against the Python float
    # would fail on the rounding rather than on the clamp.
    cap = float(np.float32(reverb_mod.MAX_FEEDBACK))
    hostile = {"runaway": 1.10, "absurd": 4.0, "at the edge": 1.0715, "negative": -3.0}
    outcomes = {}
    for label, roomsize in hostile.items():
        REVERB_PRESETS["_probe"] = dict(roomsize=np.float32(roomsize),
                                        damp=np.float32(0.5), wet=np.float32(0.3))
        try:
            err = _raises(guarded.set_preset, "_probe")
        finally:
            del REVERB_PRESETS["_probe"]
        fb, tail = float(guarded.feedback), guarded.tail_frames
        outcomes[label] = (
            err, fb, tail,
            # No exception, a feedback the recursion cannot run away with, and a
            # tail bound that exists: positive while the tank recirculates at all,
            # zero only when the clamp has taken the feedback out entirely.
            err is None and 0.0 <= fb <= cap
            and (tail > 0 if fb > 0.0 else tail == 0))
    check("a preset that asks for an infinite tank is clamped, not divided by zero",
          all(ok for _e, _f, _t, ok in outcomes.values())
          and reverb_mod.MAX_FEEDBACK < 1.0
          and outcomes["runaway"][1] == cap,
          "; ".join(f"{k}: {e.__class__.__name__ if e else 'ok'} fb={f:.4f} "
                    f"tail={t}" for k, (e, f, t, _ok) in outcomes.items()))

    guarded.set_preset("hall")
    loud_block = np.full((512, 2), 0.4, dtype=np.float32)
    guarded.process(loud_block)
    check("...and a clamped tank still decays, so the idle gate can bound it",
          guarded.tail_frames > 0 and np.isfinite(loud_block).all()
          and float(np.abs(loud_block).max()) < 4.0,
          f"tail={guarded.tail_frames} frames, "
          f"peak={float(np.abs(loud_block).max()):.3f}")

    # Every coefficient the comb/wet path multiplies a float32 block with has to
    # be np.float32: one Python float in there promotes the whole block to
    # float64 and doubles the memory traffic of the reverb on the audio thread.
    coeffs = {"feedback": guarded.feedback, "damp": guarded.damp, "wet": guarded.wet}
    preset_types = {f"{name}.{key}": type(value).__name__
                    for name, preset in REVERB_PRESETS.items()
                    for key, value in preset.items()
                    if not isinstance(value, np.float32)}
    block32 = np.zeros((CHUNK * 3, 2), dtype=np.float32)
    block32[0] = 0.5
    guarded.process(block32)
    buf_dtypes = {line.buf.dtype for line in (guarded._combs_l + guarded._combs_r
                                              + guarded._aps_l + guarded._aps_r)}
    store_types = {type(c.store) for c in guarded._combs_l + guarded._combs_r}
    check("the reverb's coefficients are float32, so nothing promotes the block",
          all(isinstance(v, np.float32) for v in coeffs.values())
          and not preset_types and block32.dtype == np.float32
          and buf_dtypes == {np.dtype(np.float32)} and store_types == {np.float32},
          f"{ {k: type(v).__name__ for k, v in coeffs.items()} }, "
          f"presets={preset_types or 'all float32'}, lines={buf_dtypes}, "
          f"store={ {t.__name__ for t in store_types} }")
    # ...proved on the arithmetic rather than on the declared dtype: a float64
    # coefficient shows up as a float64 *result*, which is what costs the traffic.
    probe = np.float32(0.25)
    promoted = {name: (probe * value).dtype for name, value in coeffs.items()}
    check("...and multiplying a float32 sample by any of them stays float32",
          all(dt == np.float32 for dt in promoted.values()),
          str({k: str(v) for k, v in promoted.items()}))

    # reset_reverb() must only *queue* the clear: zeroing the numpy delay lines
    # from the main thread while the callback is inside process() is a data race.
    eng = AudioEngine(bank, volume=1.0, reverb_preset="hall")
    for note in (48, 60, 67):
        eng.note_on(note, 120)
    for _ in range(40):
        eng.render(1024)
    eng.all_notes_off(immediate=True)
    check("reverb keeps ringing after the notes are cut",
          rms(eng.render(1024)) > 1e-5)
    eng.reset_reverb()
    check("reset_reverb() does not touch the delay lines from the caller",
          lines(eng.reverb) > 0.0 and eng._pending_reverb_reset,
          f"{lines(eng.reverb):.2e}")
    tail = eng.render(1024)
    check("the audio thread consumes the reset flag and the tail is gone",
          rms(tail) == 0.0 and not eng._pending_reverb_reset
          and lines(eng.reverb) == 0.0, f"rms={rms(tail):.2e}")

    # set_reverb() publishes the name for the menu at once but leaves the
    # coefficient switch to the audio thread.
    eng.set_reverb("room")
    check("set_reverb queues the switch for the audio thread",
          eng.reverb.name == "room" and eng._pending_reverb == "room"
          and eng.reverb.wet == REVERB_PRESETS["hall"]["wet"],
          f"pending={eng._pending_reverb}")
    eng.render(256)
    check("the queued preset is applied in render()",
          eng._pending_reverb is None
          and eng.reverb.wet == REVERB_PRESETS["room"]["wet"],
          f"wet={eng.reverb.wet}")


# ----------------------------------------------------------------- recorder
def make_performance(seconds: float = 12.0, seed: int = 7):
    """A pseudo-random but musical event stream, as if played on a keyboard."""
    rng = random.Random(seed)
    scale = [0, 2, 4, 5, 7, 9, 11]
    events = []
    t = 0.0
    while t < seconds:
        if rng.random() < 0.25:
            events.append((t, CC, 64, 127))
        chord = rng.random() < 0.3
        pitches = []
        root = 48 + rng.choice(scale) + 12 * rng.randint(0, 2)
        pitches.append(root)
        if chord:
            pitches += [root + 4, root + 7]
        dur = rng.choice([0.2, 0.35, 0.5, 0.8])
        for p in pitches:
            vel = rng.randint(35, 120)
            events.append((t, NOTE_ON, p, vel))
            events.append((t + dur, NOTE_OFF, p, 0))
        if rng.random() < 0.25:
            events.append((t + dur, CC, 64, 0))
        t += rng.choice([0.15, 0.25, 0.4])
    events.sort(key=lambda e: e[0])
    return events


def test_recorder(bank: SampleBank) -> None:
    print("\n[4] recorder + export")
    recorder = rec.Recorder()

    events = make_performance()
    # Feed half before hitting record, half after: session should hold both.
    split = len(events) // 2
    for e in events[:split]:
        recorder.handle(e[1], e[2], e[3], e[0])
    recorder.start()
    for e in events[split:]:
        recorder.handle(e[1], e[2], e[3], e[0])
    recorder.stop()

    take = recorder.snapshot("take")
    session = recorder.snapshot("session")
    check("take holds only post-record events", len(take) == len(events) - split,
          f"{len(take)}/{len(events) - split}")
    check("session holds everything", len(session) == len(events),
          f"{len(session)}/{len(events)}")

    expected_notes = sum(1 for e in events if e[1] == NOTE_ON and e[3] > 0)
    check("session note count matches", rec.Recorder.note_count(session) == expected_notes,
          f"{rec.Recorder.note_count(session)}/{expected_notes}")

    # stats() is what the 2 Hz menu refresh reads: the counters it returns must
    # agree with a full rescan of the buffers it refuses to walk.
    stats = recorder.stats()
    check("stats() is a RecorderStats triple",
          isinstance(stats, rec.RecorderStats)
          and stats._fields == ("take_notes", "take_seconds", "session_notes"),
          str(stats))
    check("stats() note counts match a full rescan",
          stats.take_notes == rec.Recorder.note_count(take)
          and stats.session_notes == expected_notes,
          f"{stats.take_notes}/{rec.Recorder.note_count(take)} take, "
          f"{stats.session_notes}/{expected_notes} session")
    check("stats() take span matches the take's own timestamps",
          abs(stats.take_seconds - rec.Recorder.duration(take)) < 1e-9,
          f"{stats.take_seconds:.3f}s vs {rec.Recorder.duration(take):.3f}s")

    recorder.discard()
    check("discard clears the take", recorder.snapshot("take") == [])
    empty = recorder.stats()
    check("stats() resets with the take but keeps the session",
          empty.take_notes == 0 and empty.take_seconds == 0.0
          and empty.session_notes == expected_notes, str(empty))

    tmpdir = tempfile.mkdtemp(prefix="justpiano-test-")
    # The artefact path is printed at the end so the WAV can be auditioned, but
    # nothing ever removed it: every run used to leak ~2.7 MB under /tmp (57
    # leftover directories, >150 MB, accumulated during one review).
    if not _os.environ.get("JUSTPIANO_KEEP_ARTIFACTS"):
        _atexit.register(_shutil.rmtree, tmpdir, ignore_errors=True)

    # ---- MIDI round-trip ----
    import mido
    mid_path = os.path.join(tmpdir, "take.mid")
    rec.export_midi(session, mid_path)
    check("midi file written", os.path.getsize(mid_path) > 100,
          f"{os.path.getsize(mid_path)} bytes")

    reloaded = mido.MidiFile(mid_path)
    on_msgs = [m for tr in reloaded.tracks for m in tr
               if m.type == "note_on" and m.velocity > 0]
    off_msgs = [m for tr in reloaded.tracks for m in tr
                if m.type == "note_off" or (m.type == "note_on" and m.velocity == 0)]
    check("note_on count survives the round-trip", len(on_msgs) == expected_notes,
          f"{len(on_msgs)}/{expected_notes}")
    check("every note is switched off", len(off_msgs) >= len(on_msgs),
          f"{len(off_msgs)} offs for {len(on_msgs)} ons")
    check("velocities are preserved",
          [m.velocity for m in on_msgs] == [e[3] for e in events
                                            if e[1] == NOTE_ON and e[3] > 0])

    src_dur = rec.Recorder.duration(session)
    # The exporter is deterministic: this take ends with the pedal already up
    # and no hanging notes, so the only error is the 1 ms tick quantisation.
    # (The old bound was 0.35 s -- seven times the "+-50 ms" the label promised.)
    check("timing is preserved (±10 ms)", abs(reloaded.length - src_dur) < 0.01,
          f"{reloaded.length:.3f}s vs {src_dur:.3f}s")

    pedal = [m for tr in reloaded.tracks for m in tr
             if m.type == "control_change" and m.control == 64]
    check("sustain pedal is recorded", len(pedal) >= 2, f"{len(pedal)} pedal events")
    check("file opens as a valid type-0 SMF", reloaded.type == 0
          and reloaded.ticks_per_beat == rec.TICKS_PER_BEAT)

    # A pedal left down at the end must be released -- on the channel that put
    # it down, not on channel 0. Keyboards that transmit on channel 4 exist.
    chan = 3
    held_pedal = [(0.0, NOTE_ON | chan, 60, 100),
                  (0.1, CC | chan, 64, 127),
                  (0.5, NOTE_OFF | chan, 60, 0)]
    ped_path = os.path.join(tmpdir, "pedal-chan.mid")
    rec.export_midi(held_pedal, ped_path)
    ped_mid = mido.MidiFile(ped_path)
    ups = [m for tr in ped_mid.tracks for m in tr
           if m.type == "control_change" and m.control == 64 and m.value < 64]
    check("a pedal left down is released on its own channel",
          len(ups) == 1 and ups[0].channel == chan,
          f"{[(m.channel, m.value) for m in ups]}")
    check("the pedal-up tail is 50 ms after the last event",
          abs(ped_mid.length - (0.5 + 0.05)) < 0.01, f"{ped_mid.length:.3f}s")

    # hanging notes must be closed by the exporter
    hanging = [(0.0, NOTE_ON, 60, 100), (1.0, NOTE_ON, 64, 100)]
    hang_path = os.path.join(tmpdir, "hanging.mid")
    rec.export_midi(hanging, hang_path)
    hang = mido.MidiFile(hang_path)
    offs = [m for tr in hang.tracks for m in tr
            if m.type == "note_off" or (m.type == "note_on" and m.velocity == 0)]
    check("hanging notes get note_off appended", len(offs) == 2, f"{len(offs)}")

    # Two rtmidi callback threads (hardware port + virtual port) can hand events
    # to Recorder.handle() out of order, so _normalise() has to sort *before* it
    # picks the time origin and appends the end-of-take markers -- otherwise the
    # generated note_off lands before the note_on it is supposed to close. A
    # plain out-of-order list does not discriminate: the trailing out.sort()
    # repairs that, so the case has to combine reordering with a hanging note.
    jumbled = [(0.9, NOTE_ON, 67, 90), (0.0, NOTE_ON, 60, 100),
               (0.2, NOTE_OFF, 60, 0)]
    jum_path = os.path.join(tmpdir, "jumbled.mid")
    rec.export_midi(jumbled, jum_path)
    jum = mido.MidiFile(jum_path)
    jum_notes = [(m.type, m.note) for tr in jum.tracks for m in tr
                 if m.type in ("note_on", "note_off")]
    check("out-of-order events are sorted before the end-of-take markers",
          jum_notes == [("note_on", 60), ("note_off", 60),
                        ("note_on", 67), ("note_off", 67)],
          str(jum_notes))

    # Pitch bend is accepted by Recorder.handle() but was never exported by any
    # test, so every bend could have been silently dropped from saved MIDI.
    bender = rec.Recorder()
    bender.start()
    bender.handle(NOTE_ON, 60, 100, 0.0)
    bender.handle(rec.PITCH_BEND, 0x00, 0x60, 0.2)      # (0x60 << 7) - 8192
    bender.handle(NOTE_OFF, 60, 0, 0.5)
    bender.stop()
    bend_path = os.path.join(tmpdir, "bend.mid")
    rec.export_midi(bender.snapshot("take"), bend_path)
    wheels = [m for tr in mido.MidiFile(bend_path).tracks for m in tr
              if m.type == "pitchwheel"]
    check("pitch bend survives the MIDI round-trip",
          len(wheels) == 1 and wheels[0].pitch == 4096 and wheels[0].channel == 0,
          str([(w.channel, w.pitch) for w in wheels]))

    # The session buffer is a ring buffer: this app is meant to run all day, so
    # it must stay bounded and keep the *most recent* events.
    ring = rec.Recorder()
    overflow = rec.SESSION_LIMIT + 100
    for i in range(overflow):
        ring.handle(NOTE_ON, 21 + i % 88, 100, float(i))
    ring_session = ring.snapshot("session")
    check("the session buffer is trimmed instead of growing without bound",
          len(ring_session) == rec.SESSION_LIMIT,
          f"{len(ring_session)}/{rec.SESSION_LIMIT} of {overflow}")
    check("the trim drops the oldest events and keeps the newest",
          ring_session[0][0] == float(overflow - rec.SESSION_LIMIT)
          and ring_session[-1][0] == float(overflow - 1),
          f"{ring_session[0][0]}..{ring_session[-1][0]}")
    check("the note counter is corrected by the trim",
          ring.stats().session_notes == len(ring_session),
          f"{ring.stats().session_notes} vs {len(ring_session)}")
    # Eviction used to delete the front quarter of a 400,000-element list and
    # rescan it for note-ons, 8.7 ms on the rtmidi callback thread while holding
    # the recorder lock. Nothing may scale with the buffer's length any more, so
    # the same measurement is taken on a buffer 100x smaller: both are at their
    # cap and neither is still growing, so any rescan shows up as a ratio.
    Ring = type(rec.Recorder().session)

    def worst_at_cap(limit, n=30_000):
        buf = Ring(limit)
        for i in range(limit):
            buf.append(float(i), NOTE_ON, 60, 100)
        worst = 0.0
        for i in range(n):
            t0 = time.perf_counter()
            buf.append(1e6 + i, NOTE_ON, 60, 100)
            worst = max(worst, time.perf_counter() - t0)
        return worst

    small_worst = worst_at_cap(4096)
    full_worst = worst_at_cap(rec.SESSION_LIMIT)
    check("recording an event at the session limit is O(1), not a rescan",
          full_worst < max(small_worst * 8.0, 200e-6),
          f"{full_worst * 1e6:.1f} us at the 400k cap vs "
          f"{small_worst * 1e6:.1f} us at a 4k one")
    check("the ring stops reallocating once it is at the cap",
          ring.session.capacity == rec.SESSION_LIMIT
          and len(ring.session) == rec.SESSION_LIMIT,
          f"capacity={ring.session.capacity}")
    # ...and the compact representation is the other half of the win: a list of
    # 400,000 tuples is 46 MiB, as much as the whole sample bank.
    per_event = (ring.session._when.nbytes + ring.session._status.nbytes
                 + ring.session._d1.nbytes + ring.session._d2.nbytes) / rec.SESSION_LIMIT
    check("a held session event costs a handful of bytes, not a boxed tuple",
          per_event <= 12.0, f"{per_event:.1f} bytes/event")
    # An empty recorder must not pre-pay for the cap it is allowed to reach:
    # an absolute bound, not `<= RING_START`, which would move with the code.
    idle_bytes = per_event * rec.Recorder().session.capacity
    check("an idle session buffer is small until it is played into",
          idle_bytes <= 64 * 1024 and rec.SESSION_LIMIT >= 400_000,
          f"{rec.Recorder().session.capacity} events, {idle_bytes / 1024:.0f} KB")
    # The counters must survive the wrap-around, not just the first lap.
    wrap = rec.Recorder()
    for i in range(rec.SESSION_LIMIT + 1000):
        wrap.handle(NOTE_ON if i % 2 else NOTE_OFF, 60, 100 if i % 2 else 0, float(i))
    wrapped = wrap.snapshot("session")
    check("the incremental note tally still matches a full rescan after a wrap",
          wrap.stats().session_notes == rec.Recorder.note_count(wrapped)
          and len(wrapped) == rec.SESSION_LIMIT,
          f"{wrap.stats().session_notes} vs "
          f"{rec.Recorder.note_count(wrapped)} of {len(wrapped)}")
    check("events survive the wrap in order and unaltered",
          wrapped[0] == (1000.0, NOTE_OFF, 60, 0)
          and wrapped[-1] == (float(rec.SESSION_LIMIT + 999), NOTE_ON, 60, 100),
          f"{wrapped[0]} .. {wrapped[-1]}")

    # ---- what snapshot() holds the lock for --------------------------------
    # `_lock` is the lock the rtmidi callback thread takes in `handle()`, and
    # `snapshot()` used to hold it for the whole conversion: 400,000 events boxed
    # into tuples is 45-61 ms, during which every arriving MIDI byte queues. The
    # measured stall was up to 70 ms of late notes per export. Only the index
    # arithmetic and the four array copies may be inside it now.
    locked = lock_spy(ring._lock)
    ring._lock = locked
    spent = []
    for _ in range(5):
        t0 = time.perf_counter()
        snap = ring.snapshot("session")
        spent.append(time.perf_counter() - t0)
    ring._lock = locked.lock
    held = sorted(locked.holds)
    median_hold = held[len(held) // 2]
    whole = sorted(spent)[len(spent) // 2]
    # 5 ms is the bound; the median is the estimator, because a scheduler quantum
    # landing inside the critical section inflates the worst case on a loaded
    # machine and that is not what regressed. The old shape was 45-61 ms *every*
    # time, an order of magnitude clear of either number.
    check("snapshot() at the 400k cap holds the lock for under 5 ms",
          median_hold < 5e-3 and held[-1] < 15e-3
          and len(snap) == rec.SESSION_LIMIT and locked.total == 5,
          f"{median_hold * 1e3:.2f} ms median, {held[-1] * 1e3:.2f} ms worst of "
          f"{len(held)}; {whole * 1e3:.1f} ms for the whole snapshot of "
          f"{len(snap)} events")
    # The comparison is the point: the expensive half has to be *outside*. A
    # snapshot that holds the lock for most of its own runtime is the old shape,
    # whatever the absolute numbers on the machine of the day.
    check("...and the boxing into tuples is left outside the lock",
          median_hold < whole * 0.25 and whole > 1e-3,
          f"{median_hold / whole:.1%} of the snapshot was locked")
    # ...which is what the rtmidi thread actually feels, and the only way to say
    # that without measuring the GIL instead of the lock is throughput: how many
    # events a MIDI thread gets through while four full-session exports run,
    # against what it manages with the lock to itself. Boxing under the lock left
    # it 5 % of the time; the events do not queue up anywhere, they arrive late.
    def midi_rate(seconds: float = 0.0, snapshots: int = 0) -> tuple[float, float]:
        """Events/s a MIDI thread manages, and over how long. The window is
        exactly the export phase when there is one, so the measurement is not
        diluted by time in which nothing was competing for anything."""
        stop_midi = threading.Event()
        count = [0]

        def feed() -> None:
            i = 0
            while not stop_midi.is_set():
                ring.handle(NOTE_ON, 60, 100, float(1e7 + i))
                count[0] += 1
                i += 1

        feeder = threading.Thread(target=feed, name="fake-rtmidi")
        feeder.start()
        time.sleep(0.02)                       # let it get going
        t0 = time.perf_counter()
        for _ in range(snapshots):
            ring.snapshot("session")
        while time.perf_counter() - t0 < seconds:
            time.sleep(0.002)
        window = time.perf_counter() - t0
        got = count[0]
        stop_midi.set()
        feeder.join(30.0)
        return got / window, window

    busy_rate, window = midi_rate(snapshots=6)
    quiet_rate, _ = midi_rate(seconds=window)
    check("MIDI keeps flowing while six full-session exports run",
          busy_rate > quiet_rate * 0.25 and quiet_rate > 1000,
          f"{busy_rate:.0f} events/s over the {window * 1e3:.0f} ms of exports vs "
          f"{quiet_rate:.0f} idle ({busy_rate / max(quiet_rate, 1e-9):.0%})")

    # ---- the timestamp is taken inside the lock ----------------------------
    # Two rtmidi callback threads are normal (the hardware port and the always-on
    # virtual one). Sampling the clock before queueing at the lock let the loser
    # of the race append the *older* timestamp last, which breaks the oldest-first
    # contract `snapshot()` sells to the exporters and made `duration()` -- the
    # length in the export notification -- come out negative.
    ordered = rec.Recorder()
    ordered.start()
    stamped = threading.Barrier(4)

    def two_ports(n: int = 6000) -> None:
        stamped.wait()
        for i in range(n):
            ordered.handle(NOTE_ON if i % 2 else NOTE_OFF, 60, 100 if i % 2 else 0)

    ports = [threading.Thread(target=two_ports, name=f"port-{i}") for i in range(4)]
    for t in ports:
        t.start()
    for t in ports:
        t.join(60.0)
    ordered.stop()
    live = ordered.snapshot("session")
    stamps = [e[0] for e in live]
    backwards = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
    check("timestamps taken inside the lock come out genuinely oldest-first",
          len(live) == 24000 and backwards == 0
          and stamps == sorted(stamps),
          f"{backwards} events out of order in {len(live)} from 4 ports")
    check("...so duration() and stats() are never negative",
          rec.Recorder.duration(live) >= 0.0
          and ordered.stats().take_seconds >= 0.0
          and ordered.take.span() >= 0.0
          and rec.Recorder.duration(live) == stamps[-1] - stamps[0],
          f"{rec.Recorder.duration(live) * 1e3:.1f} ms span")
    # A caller that supplies its own `when` is not ordered by the lock (the test
    # suites and an imported performance do), so the clamp has to be there too.
    reversed_pair = [(5.0, NOTE_ON, 60, 100), (1.0, NOTE_OFF, 60, 0)]
    check("a caller-supplied timeline that runs backwards still reads as 0.0",
          rec.Recorder.duration(reversed_pair) == 0.0
          and rec.Recorder.duration([]) == 0.0,
          f"{rec.Recorder.duration(reversed_pair)}")

    # ---- the two export constants ------------------------------------------
    # Both sides of every round-trip above read these: mido honours the tempo the
    # exporter wrote, so 240 PPQ at 60 BPM reloads at exactly the right length
    # while halving the timing resolution and writing a tempo map nothing else
    # expects. Pin them to the file's own bytes.
    tick_seconds = 60.0 / (rec.EXPORT_BPM * rec.TICKS_PER_BEAT)
    check("the exporter is 480 PPQ at 120 BPM, i.e. ~1 ms per tick",
          rec.TICKS_PER_BEAT == 480 and rec.EXPORT_BPM == 120.0
          and tick_seconds <= 0.0011,
          f"{rec.TICKS_PER_BEAT} PPQ, {rec.EXPORT_BPM} BPM, "
          f"{tick_seconds * 1e3:.3f} ms/tick")
    beat = [(0.0, NOTE_ON, 60, 100), (1.0, NOTE_OFF, 60, 0)]
    beat_path = os.path.join(tmpdir, "one-beat.mid")
    rec.export_midi(beat, beat_path)
    beat_mid = mido.MidiFile(beat_path)
    tempos = [m for tr in beat_mid.tracks for m in tr if m.type == "set_tempo"]
    deltas = [m.time for tr in beat_mid.tracks for m in tr
              if m.type in ("note_on", "note_off")]
    check("...and the file says so: header ticks and one tempo meta event",
          beat_mid.ticks_per_beat == 480 and len(tempos) == 1
          and tempos[0].tempo == mido.bpm2tempo(120.0)
          and abs(mido.tempo2bpm(tempos[0].tempo) - 120.0) < 1e-9,
          f"{beat_mid.ticks_per_beat} PPQ, {tempos[0].tempo} us/beat "
          f"= {mido.tempo2bpm(tempos[0].tempo):.1f} BPM")
    check("...so one second of performance is 960 ticks in the file",
          deltas[:2] == [0, 960],
          f"deltas {deltas[:2]} for a 1.000 s note")

    # ---- WAV render ----
    # An unfinished bank must be refused, not rendered: AudioEngine.note_on()
    # drops notes whose samples do not exist yet, so this used to write a WAV
    # with part of the performance silently missing.
    unbuilt_path = os.path.join(tmpdir, "unbuilt.wav")
    unbuilt_error = None
    try:
        rec.export_wav(session, SampleBank(), unbuilt_path)
    except Exception as exc:
        unbuilt_error = exc
    check("export_wav refuses to render from a bank that is still building",
          isinstance(unbuilt_error, ValueError)
          and "still being built" in str(unbuilt_error)
          and not os.path.exists(unbuilt_path),
          f"{unbuilt_error!r} file={os.path.exists(unbuilt_path)}")
    empty_error = None
    try:
        rec.export_wav([], bank, os.path.join(tmpdir, "empty.wav"))
    except Exception as exc:
        empty_error = exc
    check("export_wav refuses an empty performance",
          isinstance(empty_error, ValueError) and "nothing to render" in str(empty_error),
          repr(empty_error))

    wav_path = os.path.join(tmpdir, "take.wav")
    t0 = time.time()
    rec.export_wav(session, bank, wav_path, reverb="room", volume=0.9)
    render_time = time.time() - t0

    with wave.open(wav_path) as wav:
        frames = wav.getnframes()
        sr = wav.getframerate()
        channels = wav.getnchannels()
        audio = np.frombuffer(wav.readframes(frames), dtype="<i2")
    audio = audio.reshape(-1, 2) / 32768.0

    check("wav is 44.1 kHz stereo", sr == 44100 and channels == 2)
    check("wav duration matches performance",
          abs(frames / sr - (src_dur + 3.0)) < 0.5,
          f"{frames / sr:.2f}s vs {src_dur + 3.0:.2f}s")
    check("wav contains signal", rms(audio) > 0.005, f"rms={rms(audio):.4f}")
    check("wav is not clipped", np.abs(audio).max() < 0.999,
          f"peak={np.abs(audio).max():.3f}")
    check("wav render is faster than realtime",
          render_time < frames / sr, f"{render_time:.2f}s for {frames / sr:.1f}s audio")

    # audio must actually stop after the tail
    tail = audio[-int(0.3 * sr):]
    check("wav ends in silence", rms(tail) < 1e-3, f"rms={rms(tail):.2e}")

    print(f"\n  artifacts: {tmpdir}"
          + ("" if _os.environ.get("JUSTPIANO_KEEP_ARTIFACTS")
             else " (removed at exit; set JUSTPIANO_KEEP_ARTIFACTS=1 to keep)"))


# ------------------------------------------------------------ output stream
def test_output_stream(bank: SampleBank) -> None:
    """AudioEngine.start()/stop() against a stub PortAudio.

    `sounddevice` is imported lazily inside start(), so the whole lifecycle --
    including the failure paths that no Linux machine can reach with the real
    library -- is testable by putting a stub in sys.modules.
    """
    print("\n[5] audio output stream")
    import types

    opened: list = []
    closed: list = []
    mode = {"fail": ""}          # "" | "construct" | "start"

    class _Stream:
        def __init__(self, **kw):
            if mode["fail"] == "construct":
                raise RuntimeError("Error opening OutputStream: "
                                   "Invalid device [PaErrorCode -9996]")
            self.device = kw.get("device")
            opened.append(self.device)

        def start(self):
            if mode["fail"] == "start":
                raise RuntimeError("Error starting OutputStream: "
                                   "Device unavailable [PaErrorCode -9985]")

        def stop(self):
            pass

        def close(self):
            closed.append(self.device)

    sd = types.ModuleType("sounddevice")
    sd.OutputStream = lambda **kw: _Stream(**kw)
    sd.query_devices = lambda idx=None: [] if idx is None else {}
    sd.default = types.SimpleNamespace(device=(0, 0))
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = sd
    try:
        engine = AudioEngine(bank, blocksize=256, reverb_preset="off")
        check("start() opens the requested device and clears the error",
              engine.start(1) is True and engine.stream is not None
              and opened == [1] and engine.error is None,
              f"opened={opened} error={engine.error!r}")
        engine.stop()
        check("stop() releases the stream", engine.stream is None and closed == [1],
              f"closed={closed}")

        # PortAudio hands back a live stream object and only *then* refuses to
        # start it. sounddevice's _StreamBase has no finalizer, so dropping the
        # reference leaks the PortAudio stream for the life of the process.
        mode["fail"] = "start"
        opened.clear()
        closed.clear()
        started = engine.start(2)
        check("a stream that fails to start is closed, not leaked",
              started is False and engine.stream is None
              and opened == [2] and closed == [2],
              f"started={started} opened={opened} closed={closed}")
        check("the PortAudio reason is kept for the UI",
              bool(engine.error) and "PaErrorCode" in (engine.error or ""),
              repr(engine.error))

        # A constructor that raises leaves nothing to close: the failure path
        # must not blow up on the absent stream either.
        mode["fail"] = "construct"
        opened.clear()
        closed.clear()
        raised = None
        try:
            started = engine.start(3)
        except Exception as exc:
            raised = exc
        check("a device that cannot even be opened is reported, not raised",
              raised is None and started is False and engine.stream is None
              and opened == [] and closed == [] and bool(engine.error),
              f"{raised!r} closed={closed}")
    finally:
        if saved is None:
            sys.modules.pop("sounddevice", None)
        else:
            sys.modules["sounddevice"] = saved


# ---------------------------------------------------------- native helpers
def test_native_helpers() -> None:
    """macui's AppleScript quoting and Settings.save()'s error reporting.

    Both are macOS-only in effect but pure string/IO logic in code: an unescaped
    quote in a filename turns the whole `osascript -e` program into a syntax
    error (the save panel silently never appears), and a settings write that
    fails has to leave a reason behind for the status line.
    """
    print("\n[6] native helpers")
    from justpiano import config, macui

    check("_as_applescript escapes backslashes, quotes and newlines",
          macui._as_applescript('a\\b "c"\nd\re') == 'a\\\\b \\"c\\"\\nd\\re',
          macui._as_applescript('a\\b "c"\nd\re'))

    scripts: list[str] = []
    real_osascript = macui._osascript

    def fake_osascript(script, timeout=60.0):
        scripts.append(script)
        return fake_osascript.reply

    fake_osascript.reply = "/tmp/chosen.mid"
    macui._osascript = fake_osascript
    try:
        # AppKit is absent here, so save_panel falls through to AppleScript --
        # the same path a Mac takes when NSSavePanel is unavailable.
        nasty_dir = _os.path.join(_HOME, 'a "quoted" dir')
        chosen = macui.save_panel('My "best" take.mid', nasty_dir, "mid",
                                  'Save "it" now')
        script = scripts[-1] if scripts else ""
        check("save_panel returns the path AppleScript reported",
              chosen == "/tmp/chosen.mid", repr(chosen))
        check("save_panel escapes the prompt",
              'with prompt "Save \\"it\\" now"' in script, script.splitlines()[0][:80])
        check("save_panel escapes the default name",
              'default name "My \\"best\\" take.mid"' in script,
              script.splitlines()[0][-60:])
        check("save_panel escapes the directory",
              f'POSIX file "{macui._as_applescript(nasty_dir)}"' in script)
        check("no raw quote survives into the AppleScript literals",
              'name "My "best"' not in script and f'file "{nasty_dir}"' not in script)

        # is_login_item is tri-state: None means "System Events never answered",
        # which the caller must not read as "not registered".
        bundle = '/Applications/Piano "Tray".app'
        fake_osascript.reply = "1"
        listed = macui.is_login_item(bundle)
        check("a positive count means the bundle is registered", listed is True,
              repr(listed))
        check("the login-item query matches on the escaped bundle path",
              f'path is "{macui._as_applescript(bundle)}"' in scripts[-1],
              scripts[-1][-60:])
        fake_osascript.reply = "0"
        check("a zero count means it is not registered",
              macui.is_login_item(bundle) is False)
        fake_osascript.reply = None
        check("a failed query is None, not False",
              macui.is_login_item(bundle) is None)
        fake_osascript.reply = "not a number"
        check("an unparseable answer is None too",
              macui.is_login_item(bundle) is None)

        fake_osascript.reply = ""
        check("removing a login item deletes every entry with that path",
              macui.set_login_item(bundle, False) is True
              and "delete (every login item whose path is "
                  f'"{macui._as_applescript(bundle)}")' in scripts[-1],
              scripts[-1][-70:])
        check("adding a login item registers the escaped path",
              macui.set_login_item(bundle, True) is True
              and f'path:"{macui._as_applescript(bundle)}"' in scripts[-1],
              scripts[-1][-70:])
        fake_osascript.reply = None
        check("a refused System Events call is reported as a failure",
              macui.set_login_item(bundle, True) is False)
    finally:
        macui._osascript = real_osascript

    # Settings.save() must never raise from a menu callback, but silently
    # swallowing the error is how "my choices never stick" became invisible.
    settings = config.Settings()
    good_path = config.SETTINGS_PATH
    config.SETTINGS_PATH = _os.path.join(good_path, "no-such-dir", "settings.json")
    raised = None
    try:
        settings["volume"] = 0.42
    except Exception as exc:
        raised = exc
    check("a settings write that fails records the reason instead of raising",
          raised is None and bool(settings.error) and "settings.json" in settings.error,
          f"{raised!r} error={settings.error!r}")
    config.SETTINGS_PATH = good_path
    settings["volume"] = 0.42
    check("the error clears once a write succeeds",
          settings.error is None and _os.path.exists(good_path),
          repr(settings.error))


# ---------------------------------------------------------------- voicings
def test_voicings(grand: SampleBank) -> None:
    """Instruments: the persisted setting, the per-voicing cache, the timbres.

    `grand` is the bank test_bank() already built and cached, so only one further
    voicing is ever built here (~1.2 s); everything else reloads or works on
    single notes.
    """
    print("\n[7] instruments")
    from justpiano import config

    dark = "felt"

    # ---- the setting -----------------------------------------------------
    check("the menu can be built from the tone model: every voicing is labelled",
          tone.DEFAULT_VOICING in tone.VOICINGS and len(tone.VOICINGS) >= 2
          and all(v in tone.VOICING_LABELS for v in tone.VOICINGS)
          and len(set(tone.VOICING_LABELS[v] for v in tone.VOICINGS)) == len(tone.VOICINGS),
          f"{list(tone.VOICINGS)}")
    check("the voicing setting defaults to the tone model's default voicing",
          config.DEFAULTS.get("voicing") == tone.DEFAULT_VOICING,
          f'{config.DEFAULTS.get("voicing")!r} vs {tone.DEFAULT_VOICING!r}')

    def settings_voicing(stored_value) -> str:
        """`Settings()["voicing"]` as read back from a settings.json holding
        `stored_value` -- i.e. what a hand-edited or stale file really does."""
        saved = None
        if os.path.exists(config.SETTINGS_PATH):
            with open(config.SETTINGS_PATH) as fh:
                saved = fh.read()
        try:
            stored = json.loads(saved) if saved else {}
            stored["voicing"] = stored_value
            with open(config.SETTINGS_PATH, "w") as fh:
                json.dump(stored, fh)
            return config.Settings()["voicing"]
        finally:
            if saved is None:
                os.remove(config.SETTINGS_PATH)
            else:
                with open(config.SETTINGS_PATH, "w") as fh:
                    fh.write(saved)

    check("a stored voicing is honoured", settings_voicing(dark) == dark,
          repr(settings_voicing(dark)))
    # A voicing that no longer exists (renamed, dropped, or hand-typed) must not
    # wedge the app: the menu would show no checkmark and SampleBank would cache
    # the default's samples under the missing id's name.
    stale = [settings_voicing(v) for v in ("harpsichord-1712", "", None, 17,
                                           tone.DEFAULT_VOICING.upper())]
    check("a stale or unknown voicing falls back to the default",
          stale == [tone.DEFAULT_VOICING] * 5, str(stale))
    check("a bank asked for an unknown voicing normalises to the default",
          SampleBank("harpsichord-1712").voicing == tone.DEFAULT_VOICING
          and SampleBank("harpsichord-1712").fingerprint == grand.fingerprint)

    # ---- the render spy takes the voicing call shape ----------------------
    # `SampleBank._render()` always calls render_note with four arguments, so
    # the spy has to forward *args/**kwargs rather than a fixed signature.
    with render_spy() as spy:
        positional = tone.render_note(60, "soft", dark, 22050)
        keyword = tone.render_note(60, "soft", voicing=dark, samplerate=44100)
    check("the render spy forwards positional and keyword call shapes",
          spy.count == 2 and positional.size > 0 and keyword.size > 0
          and positional.size != keyword.size,
          f"{spy.count} calls, {positional.size} vs {keyword.size} samples")

    # ---- one cache per voicing -------------------------------------------
    expected_renders = (tone.NOTE_MAX - tone.NOTE_MIN + 1) * len(tone.LAYERS)
    t0 = time.time()
    with render_spy() as spy:
        felt = SampleBank(dark)
        felt.build_blocking()
    build_time = time.time() - t0
    check("a first-time voicing renders its own bank",
          spy.count == expected_renders and felt.ready
          and felt.get_pair(60) is not None,
          f"{spy.count}/{expected_renders} notes in {build_time:.2f}s")
    check("each voicing caches under its own files and fingerprint",
          felt._blob_path != grand._blob_path
          and felt._index_path != grand._index_path
          and felt.fingerprint != grand.fingerprint
          and dark in os.path.basename(felt._blob_path)
          and os.path.exists(felt._blob_path) and os.path.exists(grand._blob_path),
          f"{os.path.basename(felt._blob_path)} / "
          f"{os.path.basename(grand._blob_path)}")
    same = np.array_equal(felt.get(60, "hard")[:4096], grand.get(60, "hard")[:4096])
    check("the two banks really hold different audio", not same,
          f"felt {felt.get(60, 'hard').size} vs grand "
          f"{grand.get(60, 'hard').size} samples")

    # Building the second voicing must not have disturbed the first one's cache,
    # and neither reload may re-render a single note.
    with render_spy() as spy:
        t0 = time.time()
        reload_grand = SampleBank(tone.DEFAULT_VOICING)
        reload_grand.build_blocking()
        reload_felt = SampleBank(dark)
        reload_felt.build_blocking()
        reload_time = time.time() - t0
    check("neither voicing's cache invalidates the other",
          spy.count == 0 and reload_grand.ready and reload_felt.ready
          and np.array_equal(reload_grand.get(60, "hard"), grand.get(60, "hard"))
          and np.array_equal(reload_felt.get(60, "hard"), felt.get(60, "hard")),
          f"{spy.count} notes re-rendered, both reloaded in {reload_time:.2f}s")
    check("switching back to a known voicing is a file read, not a rebuild",
          reload_time < max(1.0, build_time * 0.5), f"{reload_time:.2f}s")

    # Belt and braces around the per-voicing file name: a blob renamed by hand
    # (or written by the older voicing-blind layout) must never be read as
    # another voicing's samples.
    with open(felt._index_path) as fh:
        good_index = json.load(fh)
    with open(felt._index_path, "w") as fh:
        json.dump(dict(good_index, voicing=tone.DEFAULT_VOICING), fh)
    mislabelled = SampleBank(dark)._load_cache()
    with open(felt._index_path, "w") as fh:
        json.dump(good_index, fh)
    check("a cache index naming another voicing is rejected",
          mislabelled is False and SampleBank(dark)._load_cache() is True,
          f"loaded={mislabelled}")

    # ---- five instruments, two families -----------------------------------
    def partial_hf_share(voicing, note=60, layer="hard", cut=2000.0) -> float:
        """Share of a *string* launch spectrum's energy above `cut`.

        Struck strings only: `partial_series` describes a stretched harmonic
        series, and a tine's bending modes are not one (see `electric_partials`).
        """
        idx, freqs, _phases = tone.partial_series(note, voicing)
        energy = tone.partial_amplitudes(idx, freqs, note, layer, voicing) ** 2
        return float(energy[freqs > cut].sum() / energy.sum())

    def rendered(voicing, note=60, layer="hard"):
        buf = tone.render_note(note, layer, voicing)

        def centroid(seg):
            sp = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
            f = np.fft.rfftfreq(seg.size, 1 / 44100)
            return float((sp * f).sum() / sp.sum())

        # Two windows: the whole launch, whose colour the strings decide, and
        # the first 46 ms, where the mechanical action is also sounding. `felt`
        # has to measure the darkest voicing in *both* -- with the action layer
        # at its bare absolute level the onset window came out brighter than the
        # concert grand's (2332 vs 1851 Hz), i.e. the noise, not the tone, was
        # setting the timbre.
        return centroid(buf[:16384]), centroid(buf[:2048]), buf

    def action_balance_db(voicing, notes=(48, 60, 72), layer="hard") -> float:
        """Mean peak of the action layer against the tone's, in dB.

        `render_note` peak-normalises its buffer, which scales the tone and the
        action by the same factor, so the balance is read off the two ingredients
        on their way in: the partial sum's peak is `tone_gain` by construction,
        and the noise layer is caught as it is mixed. Averaged over a few notes
        because each one draws its own noise realisation.
        """
        real = tone._mechanical_noise
        peaks = []

        def spy(*a, **kw):
            out = real(*a, **kw)
            peaks.append(float(np.max(np.abs(out))))
            return np.zeros_like(out)

        tone._mechanical_noise = spy
        try:
            for note in notes:
                tone.render_note(note, layer, voicing)
        finally:
            tone._mechanical_noise = real
        gain = tone.VOICINGS[voicing]["tone_gain"]
        return float(np.mean([20.0 * np.log10(p / gain) for p in peaks]))

    def action_level_db(voicing, velocity, noise_bias=0.0) -> float:
        """Peak level of the action layer at one blow strength.

        Called straight through `_mechanical_noise` with a fixed generator, so
        every strength sees the *same* noise realisation and the only difference
        left is the level -- the per-note/layer seeds `render_note` uses are
        worth a couple of dB of their own.
        """
        noise = tone._mechanical_noise(8192, 44100, tone.VOICINGS[voicing],
                                       velocity, noise_bias,
                                       np.random.default_rng(7))
        return float(20.0 * np.log10(max(float(np.max(np.abs(noise))), 1e-12)))

    def band_peak(buf, lo, hi, t0=0.0, t1=0.12) -> float:
        seg = buf[int(t0 * 44100):int(t1 * 44100)]
        if seg.size < 512:
            return 1e-12
        spectrum = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        freqs = np.fft.rfftfreq(seg.size, 1 / 44100)
        band = (freqs >= lo) & (freqs < hi)
        return float(spectrum[band].max()) if band.any() else 1e-12

    def bell_db(voicing, note=48, layer="hard", t0=0.0, t1=0.12) -> float:
        """The bar's first bending mode against the fundamental, in dB.

        This is the tine bark, and it is the whole difference between an electric
        piano and a filtered sawtooth: a clamped-free bar rings at 6.267x its
        fundamental, nowhere near a harmonic, and it is gone within a few hundred
        milliseconds while the fundamental sings on.
        """
        buf = tone.render_note(note, layer, voicing)
        f0 = tone.midi_to_freq(note)
        ratio = float(tone.VOICINGS[voicing]["bar_ratios"][0])
        bell = band_peak(buf, f0 * ratio * 0.92, f0 * ratio * 1.08, t0, t1)
        fund = max(band_peak(buf, f0 * 0.7, f0 * 1.4, t0, t1), 1e-12)
        return 20.0 * np.log10(bell / fund)

    def harmonic_db(voicing, note=60, layer="hard", n=2, t0=0.0, t1=0.25) -> float:
        """Harmonic `n` against the fundamental, in dB."""
        buf = tone.render_note(note, layer, voicing)
        f0 = tone.midi_to_freq(note)
        got = band_peak(buf, f0 * (n - 0.35), f0 * (n + 0.35), t0, t1)
        fund = max(band_peak(buf, f0 * 0.7, f0 * 1.4, t0, t1), 1e-12)
        return 20.0 * np.log10(max(got, 1e-12) / fund)

    strings = [v for v in tone.VOICINGS if tone.family(v) == tone.STRING]
    electrics = [v for v in tone.VOICINGS if tone.family(v) == tone.ELECTRIC]
    check("the tone model ships five instruments in two families",
          list(tone.VOICINGS) == ["grand", "upright", "felt", "rhodes", "wurlitzer"]
          and strings == ["grand", "upright", "felt"]
          and electrics == ["rhodes", "wurlitzer"]
          and tone.DEFAULT_VOICING == "grand",
          f"{ {v: tone.family(v) for v in tone.VOICINGS} }")
    check("every voicing carries a complete parameter set, whatever it spelled out",
          all(set(tone.VOICINGS[v]) == set(tone.VOICINGS[strings[0]])
              for v in strings)
          and all(set(tone.VOICINGS[v]) == set(tone.VOICINGS[electrics[0]])
                  for v in electrics)
          and all("tone_gain" in tone.VOICINGS[v] and "gain" in tone.VOICINGS[v]
                  and "attack_ms" in tone.VOICINGS[v] for v in tone.VOICINGS),
          f"{len(tone.VOICINGS[strings[0]])} string keys, "
          f"{len(tone.VOICINGS[electrics[0]])} electric keys")

    # ---- the three struck-string voicings ---------------------------------
    shares = {v: partial_hf_share(v) for v in strings}
    check("the struck-string voicings are ordered bright to dark above 2 kHz",
          shares["grand"] > 1.7 * shares["upright"]
          and shares["upright"] > 20.0 * shares["felt"],
          " ".join(f"{v}={shares[v]:.5f}" for v in strings))
    # What makes an upright's bottom octave sound like an upright: its bass
    # strings were cut to fit the case, so their partials are stretched several
    # times as far as a nine-foot grand's. Its treble, where the speaking lengths
    # are comparable, is barely touched -- a flat multiplier would be a different
    # (and wrong) instrument.
    bass_ratio = tone.inharmonicity(21, "upright") / tone.inharmonicity(21, "grand")
    treble_ratio = tone.inharmonicity(108, "upright") / tone.inharmonicity(108, "grand")
    check("the upright's bass is far more inharmonic than the grand's, its treble hardly",
          bass_ratio > 2.5 and 1.05 < treble_ratio < 1.6
          and tone.inharmonicity(60, "felt") == tone.inharmonicity(60, "grand"),
          f"B x{bass_ratio:.1f} at A0, x{treble_ratio:.2f} at C8")

    measured = {v: rendered(v) for v in tone.VOICINGS}
    bright_c, bright_onset, bright_buf = measured["grand"]
    mid_c, mid_onset, mid_buf = measured["upright"]
    dark_c, dark_onset, dark_buf = measured["felt"]
    check("the darker string voicings render measurably darker audio, action included",
          bright_c > 1.1 * mid_c and bright_c > 1.4 * dark_c
          and mid_c > 1.1 * dark_c
          and bright_onset > mid_onset > 1.1 * dark_onset,
          " ".join(f"{v}={measured[v][0]:.0f}/{measured[v][1]:.0f} Hz"
                   for v in strings))
    balance = {v: action_balance_db(v) for v in strings}
    check("the upright rings shorter and works its action harder than the grand",
          tone.note_duration(60, "upright") < 0.9 * tone.note_duration(60, "grand")
          and balance["upright"] > balance["grand"] + 2.5,
          f"{tone.note_duration(60, 'upright'):.2f}s vs "
          f"{tone.note_duration(60, 'grand'):.2f}s, action/tone "
          f"{balance['upright']:+.1f} vs {balance['grand']:+.1f} dB")
    # Felt is a moderator piano: its action has to be clearly more present than
    # a concert grand's, and just as clearly *under* its own tone. Both bounds
    # matter -- around -3 dB, where the bare absolute noise levels land once the
    # strings come down 13 dB, the voicing is a percussive click, while a
    # grand's -14 dB throws the character away. The design target is -10..-12 dB,
    # and the level must still follow the blow.
    lp = tone._LAYER_PARAMS
    dark_hard = action_level_db("felt", lp["hard"]["velocity"],
                                lp["hard"]["noise_bias"])
    dark_soft = action_level_db("felt", lp["soft"]["velocity"],
                                lp["soft"]["noise_bias"])
    dark_pp = action_level_db("felt", 0.1)
    check("the felt voicing keeps its action noise audible but under the tone",
          tone.VOICINGS["felt"]["tone_gain"] < 0.5 * tone.VOICINGS["grand"]["tone_gain"]
          and -12.5 < balance["felt"] < -9.5
          and balance["felt"] > balance["grand"] + 2.0
          # ... and it is still a balance, not a gate: harder blows noisier.
          and dark_hard > dark_soft + 1.5 and dark_hard > dark_pp + 6.0,
          f"tone_gain {tone.VOICINGS['felt']['tone_gain']}, action/tone "
          + " ".join(f"{v}={balance[v]:+.1f} dB" for v in strings)
          + f", felt hard/soft/pp {dark_hard:+.1f}/{dark_soft:+.1f}/"
            f"{dark_pp:+.1f} dB")
    check("the felt voicing rings shorter and carries its own make-up gain",
          tone.note_duration(60, "felt") < 0.95 * tone.note_duration(60, "grand")
          and tone.note_gain(60, "felt") != tone.note_gain(60, "grand"),
          f"{tone.note_duration(60, 'felt'):.2f}s vs "
          f"{tone.note_duration(60, 'grand'):.2f}s, gain "
          f"{tone.note_gain(60, 'felt'):.2f} vs "
          f"{tone.note_gain(60, 'grand'):.2f}")

    # ---- the two electric pianos ------------------------------------------
    bells = {v: (bell_db(v, layer="hard"), bell_db(v, layer="soft"),
                 bell_db(v, layer="hard", t0=0.6, t1=0.9)) for v in electrics}
    check("an electric piano rings a bar: the bending mode is there at the "
          "attack and gone from the tail",
          all(-34.0 < hard < -6.0 and late < hard - 20.0
              for hard, _soft, late in bells.values()),
          " ".join(f"{v}: hard {b[0]:+.1f} late {b[2]:+.1f} dB"
                   for v, b in bells.items()))
    # A tine is a bare steel rod and rings like one; a Wurlitzer reed carries a
    # blob of lead solder on its tip, which is what kills its bending modes and
    # leaves the bark to the pickup's saturation instead.
    check("...and the tine bells where the lead-loaded reed does not",
          bells["rhodes"][0] > bells["wurlitzer"][0] + 8.0,
          f"rhodes {bells['rhodes'][0]:+.1f} vs "
          f"wurlitzer {bells['wurlitzer'][0]:+.1f} dB")
    check("...and the bark is velocity: a soft blow is nearly a pure tone",
          all(soft < hard - 6.0 for hard, soft, _late in bells.values()),
          " ".join(f"{v}: hard {b[0]:+.1f} soft {b[1]:+.1f} dB"
                   for v, b in bells.items()))
    # The one number that separates a hollow, reedy Wurlitzer ("closer to a
    # sawtooth", odd harmonics leading) from a glassy Rhodes (a sine with a bell
    # on it): where the third harmonic sits relative to the second.
    odd_lead = {v: harmonic_db(v, n=3) - harmonic_db(v, n=2) for v in electrics}
    check("the Wurlitzer's odd harmonics lead where the Rhodes' do not",
          odd_lead["wurlitzer"] > odd_lead["rhodes"] + 6.0
          and harmonic_db("wurlitzer", n=3) > harmonic_db("rhodes", n=3) + 6.0,
          " ".join(f"{v}: h3-h2 {odd_lead[v]:+.1f} dB" for v in electrics))
    # `_pickup`'s asymmetric term is what puts even harmonics into the tone, and
    # an even non-linearity necessarily makes a DC term too -- one that follows
    # the envelope, so it cannot simply be subtracted. `_dc_block` is what takes
    # it back out, and without it every note carries an inaudible offset that
    # eats the headroom the peak normalisation is trying to hand out.
    offsets = {}
    for v in electrics:
        buf = tone.render_note(48, "hard", v).astype(np.float64)
        offsets[v] = abs(float(buf.mean())) / max(float(np.abs(buf).max()), 1e-12)
    check("the pickup non-linearity leaves no DC offset behind",
          all(off < 1e-3 for off in offsets.values()),
          " ".join(f"{v}={offsets[v]:.2e}" for v in electrics))
    check("no two instruments render the same note",
          len({tone.render_note(60, "hard", v)[:4096].tobytes()
               for v in tone.VOICINGS}) == len(tone.VOICINGS))

    # The engine has to read the voicing off the bank it is playing, or the
    # make-up gain of every voicing but the default is silently dropped.
    seen = []
    real_gain = tone.note_gain

    def gain_spy(note, voicing=tone.DEFAULT_VOICING):
        seen.append(voicing)
        return real_gain(note, voicing)

    tone.note_gain = gain_spy
    try:
        AudioEngine(felt, reverb_preset="off").note_on(60, 100)
        AudioEngine(grand, reverb_preset="off").note_on(60, 100)
    finally:
        tone.note_gain = real_gain
    check("the engine plays every note through its own bank's voicing gain",
          seen == [dark, tone.DEFAULT_VOICING], str(seen))

    # ---- swapping the bank under a running engine ------------------------
    # What the tray's _swap_bank() does: silence first, re-point `bank` second.
    # A voice that survived would keep mixing numpy views into the bank that is
    # going away, and the note would be stuck on top of the new voicing.
    #
    # The order here is _swap_bank()'s, `stop()` included -- that call is the
    # reason this one cut may be a hard one: PortAudio has joined the callback,
    # so there is no next block to click into, and `stop()` forgets the voices
    # outright. That is also why the old assertion `rms(...) == 0.0` is still the
    # right one *here* while it was wrong for CC120/Panic (see test_engine): what
    # was missing was the `stop()`, not a looser threshold. So assert why the
    # block is silent, not just that it is -- every voice that could still be
    # holding a grand-piano buffer is dead and off both lists.
    eng = AudioEngine(grand, volume=1.0, reverb_preset="off")
    eng.control_change(64, 127)
    eng.note_on(28, 110)
    eng.render(1024)            # the voice is now on the audio thread's list
    doomed = list(eng._voices)
    eng.stop()                                  # no callback runs past here
    eng.all_notes_off(immediate=True)           # no voice holds an old buffer
    eng.reverb.reset()                          # nor a tail of the old tone
    eng.bank = felt
    quiet_block = eng.render(1024)
    check("silencing before the swap leaves nothing reading the old bank",
          len(doomed) == 1 and all(v.dead for v in doomed)
          and not eng._voices and not eng._pending
          and eng.active_voices == 0 and not eng.sustain
          and rms(quiet_block) == 0.0 and np.isfinite(quiet_block).all(),
          f"{len(doomed)} voices swept, dead={[v.dead for v in doomed]}, "
          f"{len(eng._voices)} mixing, {len(eng._pending)} pending, "
          f"rms={rms(quiet_block):.2e}")
    eng.note_on(60, 100)
    played = eng.render(8192)
    check("the engine is playable on the new voicing right after the swap",
          eng.active_voices == 1 and rms(played) > 1e-3
          and np.isfinite(played).all(),
          f"{eng.active_voices} voices, rms={rms(played):.4f}")
    # And the buffers it is mixing now are the new voicing's, not the old ones.
    voice_buf = eng._voices[0].soft
    check("the voices mix the new bank's buffers",
          voice_buf is felt.get(60, "soft")
          and voice_buf is not grand.get(60, "soft"))


# ------------------------------------------------------------- on-screen keys
def test_keyboard() -> None:
    """The on-screen keyboard's geometry, hit-testing, lights and mouse gesture.

    All of it is pure Python by design, so all of it is checked here rather than
    on a Mac: a black key half a width off, an end key that does not reach the
    edge, or a click that plays the white key *under* a sharp are invisible in a
    screenshot and unmistakable to arithmetic.
    """
    print("\n[8] on-screen keyboard")
    from justpiano import keyboard as kb

    check("the module needs nothing but the standard library",
          "AppKit" not in sys.modules and "objc" not in sys.modules)

    board = kb.Keyboard()
    notes = [key.note for key in board.keys]
    whites = board.white_keys
    blacks = board.black_keys
    check("an 88-key piano runs A0 to C8 with no gaps",
          notes == list(range(21, 109)) and kb.note_name(notes[0]) == "A0"
          and kb.note_name(notes[-1]) == "C8", f"{len(notes)} keys")
    check("52 of them are white and 36 are black",
          len(whites) == 52 and len(blacks) == 36
          and len(whites) + len(blacks) == 88, f"{len(whites)}/{len(blacks)}")
    check("the ends of the keyboard are naturals, as on a real piano",
          not kb.is_black(21) and not kb.is_black(108)
          and whites[0].note == 21 and whites[-1].note == 108)

    # ---- the white keys tile the panel edge to edge, exactly.
    check("the first white key starts at the left edge", whites[0].x == 0.0,
          repr(whites[0].x))
    check("the last white key ends at the right edge",
          abs(whites[-1].right - board.width) < 1e-9,
          f"{whites[-1].right} vs {board.width}")
    seams = [b.x - a.right for a, b in zip(whites, whites[1:])]
    check("no gap and no overlap between neighbouring naturals",
          len(seams) == 51 and max(abs(s) for s in seams) < 1e-9,
          f"{len(seams)} seams, worst {max(map(abs, seams), default=0):.2e}")
    check("every natural is the same width and full length",
          len({round(k.width, 9) for k in whites}) == 1
          and {round(k.height, 9) for k in whites} == {round(board.height, 9)})

    # ---- the black keys: narrower, shorter, on top, in the right places.
    check("sharps are narrower than naturals and clearly so",
          0.5 < blacks[0].width / whites[0].width < 0.65,
          f"{blacks[0].width / whites[0].width:.3f} of a white")
    check("sharps stop well short of the front edge",
          all(abs(k.height - board.height * kb.BLACK_LENGTH_RATIO) < 1e-9
              for k in blacks) and blacks[0].height < board.height * 0.7)
    check("every sharp starts at the far end, where the fingers reach over",
          all(k.y == 0.0 for k in blacks))
    check("no sharp hangs off either end of the keyboard",
          min(k.x for k in blacks) > 0.0
          and max(k.right for k in blacks) < board.width,
          f"{min(k.x for k in blacks):.2f} .. {max(k.right for k in blacks):.2f}")
    overlaps = [blacks[i + 1].x - blacks[i].right for i in range(len(blacks) - 1)]
    check("no two sharps touch, let alone overlap", min(overlaps) > 0.0,
          f"closest pair {min(overlaps):.3f}pt apart")
    per_class = {pc: len([k for k in blacks if k.note % 12 == pc]) for k in blacks
                 for pc in (k.note % 12,)}
    check("there is no sharp between E-F or B-C, and 8 A#s to the 7 of the rest",
          not any(k.note % 12 in (0, 2, 4, 5, 7, 9, 11) for k in blacks)
          and per_class == {10: 8, 1: 7, 3: 7, 6: 7, 8: 7}, str(per_class))

    # Each sharp must straddle the boundary between the two naturals it belongs
    # between -- the off-by-one that puts C# over the C/B seam passes every
    # "36 black keys" count there is.
    straddle = []
    for key in blacks:
        below, above = board.rect(key.note - 1), board.rect(key.note + 1)
        straddle.append(below.x < key.x < below.right < key.right < above.right)
    check("each sharp straddles the seam between its own two naturals",
          all(straddle), f"{straddle.count(False)} misplaced")

    # The real-piano property that fixes the offsets: the visible tops of the
    # naturals inside a group come out equal, which is only true if C#/D# lean
    # outwards, F#/A# lean outwards further, and G# sits centred.
    def top_width(note: int) -> float:
        key = board.rect(note)
        left, right = key.x, key.right
        for nb in (note - 1, note + 1):
            if kb.is_black(nb) and _raises(board.rect, nb) is None:
                sharp = board.rect(nb)
                if sharp.x < key.x:
                    left = max(left, sharp.right)
                else:
                    right = min(right, sharp.x)
        return right - left

    cde = [top_width(n) for n in (60, 62, 64)]
    fgab = [top_width(n) for n in (65, 67, 69, 71)]
    check("the visible tops of C, D and E come out the same width",
          max(cde) - min(cde) < 1e-9, f"{cde[0]:.4f}pt each")
    check("so do the tops of F, G, A and B",
          max(fgab) - min(fgab) < 1e-9, f"{fgab[0]:.4f}pt each")
    check("and the C-D-E tops are the wider pair, as on a real keyboard",
          cde[0] > fgab[0], f"{cde[0]:.4f} vs {fgab[0]:.4f}")
    seam = lambda note: board.rect(note - 1).right          # noqa: E731
    lean = {n % 12: round(board.rect(n).center_x - seam(n), 6) for n in (61, 63, 66, 68, 70)}
    check("G# is the one sharp centred on its seam", lean[8] == 0.0, repr(lean[8]))
    check("C# and D# lean apart around D, F# and A# around G-A",
          lean[1] < 0 < lean[3] and lean[6] < 0 < lean[10]
          and abs(lean[6]) > abs(lean[1]) > 0, str(lean))

    # ---- hit-testing.
    deep = board.height * 0.9                    # below every sharp

    def probe(note: int):
        """Which note a click in the middle of `note`'s key lands on."""
        if _raises(board.rect, note) is not None:
            return None                          # not on this keyboard at all
        key = board.rect(note)
        return board.note_at(key.center_x,
                             board.black_height * 0.5 if key.black else deep)

    round_trip = [probe(n) for n in range(21, 109)]
    check("clicking the middle of any of the 88 keys plays that key",
          round_trip == list(range(21, 109)),
          f"{sum(a != b for a, b in zip(round_trip, range(21, 109)))} wrong")
    csharp = board.rect(61)
    check("a click in the overlap belongs to the sharp, not the natural under it",
          board.note_at(csharp.center_x, csharp.bottom - 0.5) == 61
          and board.note_at(csharp.center_x, csharp.bottom + 0.5) == 60,
          f"{board.note_at(csharp.center_x, csharp.bottom - 0.5)} then "
          f"{board.note_at(csharp.center_x, csharp.bottom + 0.5)}")
    check("the sharp's own left and right edges are where it changes hands",
          board.note_at(csharp.x, 1.0) == 61
          and board.note_at(csharp.x - 0.01, 1.0) == 60
          and board.note_at(csharp.right + 0.01, 1.0) == 62)
    check("the narrow top of a natural between two sharps is still reachable",
          board.note_at((board.rect(61).right + board.rect(63).x) / 2, 1.0) == 62)
    check("the far top corner of the bottom A is A0, not off the keyboard",
          board.note_at(0.0, 0.0) == 21 and board.note_at(0.0, board.height - 0.01) == 21)
    check("the last pixel on the right belongs to top C",
          _safe(board.note_at, board.width - 0.01, 1.0) == 108,
          repr(_safe(board.note_at, board.width - 0.01, 1.0)))
    check("a point off the keyboard is nobody's key",
          board.note_at(-0.01, 10.0) is None
          and board.note_at(board.width, 10.0) is None
          and board.note_at(10.0, -0.01) is None
          and board.note_at(10.0, board.height) is None)
    check("a note outside the 88 has no rectangle at all",
          all(isinstance(_raises(board.rect, n), ValueError) for n in (20, 109))
          and isinstance(_raises(kb.white_index, 61), ValueError))

    # ---- velocity from where the key was struck.
    key60 = board.rect(60)
    soft = board.velocity_at(60, key60.y)
    hard = board.velocity_at(60, key60.bottom - 0.001)
    ladder = [board.velocity_at(60, key60.y + key60.height * f)
              for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    check("the front edge of a key is the loudest place on it",
          hard == kb.MOUSE_VELOCITY_MAX and soft == kb.MOUSE_VELOCITY_MIN,
          f"{soft} at the top, {hard} at the front")
    check("velocity rises the further down the key you click",
          ladder == sorted(ladder) and len(set(ladder)) == 5, str(ladder))
    check("a click past either end of a key still gives a legal velocity",
          board.velocity_at(60, -50.0) == kb.MOUSE_VELOCITY_MIN
          and board.velocity_at(60, 1e6) == kb.MOUSE_VELOCITY_MAX)
    check("a sharp is measured over its own shorter length",
          board.velocity_at(61, board.rect(61).bottom) == kb.MOUSE_VELOCITY_MAX
          and board.velocity_at(60, board.rect(61).bottom) < kb.MOUSE_VELOCITY_MAX)
    check("hit() reports the key and how hard it was struck",
          board.hit(key60.center_x, key60.bottom - 0.001) == (60, hard)
          and board.hit(-1.0, 0.0) is None)

    # ---- the panel can be any size; the proportions must hold.
    small = kb.Keyboard(width=520.0, height=60.0)
    check("a differently sized keyboard is the same keyboard, scaled",
          len(small.keys) == 88
          and abs(small.rect(61).x / small.width - board.rect(61).x / board.width) < 1e-12
          and abs(small.white_keys[-1].right - 520.0) < 1e-9,
          f"{small.white_width:.2f}pt naturals")
    check("a keyboard with no size is refused rather than dividing by zero",
          isinstance(_raises(kb.Keyboard, 0.0), ValueError)
          and isinstance(_raises(kb.Keyboard, 900.0, -1.0), ValueError))

    # ---- what gets painted.
    plan = kb.draw_plan(board, {60: 127})
    keys = [op for op in plan if op.kind == "key"]
    labels = [op for op in plan if op.kind == "label"]
    check("the paint plan covers all 88 keys and labels every C",
          len(keys) == 88 and len(labels) == 8
          and [op.text for op in labels][:2] == ["C1", "C2"]
          and any(op.text == "C4" and op.note == 60 for op in labels),
          f"{len(keys)} keys, {len(labels)} labels")
    check("naturals are painted first so the sharps land on top",
          all(not op.note % 12 in kb.BLACK_PITCH_CLASSES for op in keys[:52])
          and all(op.note % 12 in kb.BLACK_PITCH_CLASSES for op in keys[52:]))
    check("middle C is labelled differently from the other Cs",
          next(op.fill for op in labels if op.note == 60)
          != next(op.fill for op in labels if op.note == 72))
    plain = {op.note: op.fill for op in kb.draw_plan(board)}
    check("a sounding key is painted a different colour from a resting one",
          next(op.fill for op in keys if op.note == 60) != plain[60]
          and {op.note: op.fill for op in keys}[62] == plain[62])
    check("a key struck harder glows brighter than one brushed",
          kb.lit_color(60, 127) != kb.lit_color(60, 1)
          and kb.lit_color(61, 100) != kb.lit_color(60, 100))

    # ---- the lights: written by the MIDI thread, read by the main thread.
    lights = kb.NoteLights()
    version = lights.version
    lights.press(60, 80)
    check("pressing a key lights it and moves the version on",
          lights.is_down(60) and lights.snapshot() == {60: 80}
          and lights.version > version, f"v{version} -> v{lights.version}")
    version = lights.version
    lights.release(61)
    check("releasing a key that was never down changes nothing",
          lights.version == version and len(lights) == 1)
    lights.release(60)
    check("releasing the key puts it out", not lights.is_down(60)
          and lights.snapshot() == {} and lights.version > version)
    version = lights.version
    lights.release_all()
    check("panicking with nothing down is a no-op, not a redraw",
          lights.version == version)
    for note in (48, 52, 55):
        lights.press(note, 64)
    lights.release_all()
    check("panic puts every key back up at once", len(lights) == 0
          and lights.snapshot() == {})
    snap = lights.snapshot()
    lights.press(60, 64)
    check("a snapshot is a private copy the drawing code can keep", snap == {})

    # The real hand-over: an rtmidi-like thread hammering the lights while the
    # main thread snapshots. A missing lock shows up here as a mutated-dict
    # RuntimeError, and only here.
    lights.release_all()
    stop = threading.Event()
    errors: list[BaseException] = []

    def midi_thread() -> None:
        try:
            while not stop.is_set():
                for note in range(21, 109):
                    lights.press(note, 1 + note % 127)
                for note in range(21, 109):
                    lights.release(note)
        except BaseException as exc:                       # pragma: no cover
            errors.append(exc)

    worker = threading.Thread(target=midi_thread, daemon=True)
    worker.start()
    seen = 0
    try:
        deadline = time.time() + 0.5
        while time.time() < deadline:
            try:
                shot = lights.snapshot()
                seen = max(seen, len(shot))
                bad = [(n, v) for n, v in shot.items()
                       if not 21 <= n <= 108 or not 0 < v <= 127]
            except BaseException as exc:
                # A snapshot that is really the live dict: "changed size during
                # iteration", exactly what the drawing code would hit mid-frame.
                errors.append(exc)
                break
            if bad:
                errors.append(AssertionError(f"garbled snapshot {bad[:3]}"))
                break
    finally:
        stop.set()
        worker.join(2.0)
    check("snapshotting while a MIDI thread plays never tears or raises",
          not errors and not worker.is_alive() and seen > 0,
          f"{seen} keys caught mid-flight, errors={errors[:1]}")
    lights.release_all()

    # ---- the controller: mouse gestures and the redraw gate.
    played: list[tuple[int, int]] = []
    stopped: list[int] = []
    gear: list[int] = []
    ctl = kb.KeyboardController(board, on_note_on=lambda n, v: played.append((n, v)),
                                on_note_off=stopped.append,
                                on_settings=lambda: gear.append(1))
    check("a fresh controller draws nothing lit and holds no key",
          ctl.lit == {} and ctl.mouse_note is None)
    ctl.lights.press(60, 96)
    check("the first refresh after a note arrives asks for a redraw",
          ctl.refresh() is True and ctl.lit == {60: 96})
    check("a refresh with nothing changed does not ask again",
          ctl.refresh() is False)
    ctl.lights.release(60)
    check("and the release does", ctl.refresh() is True and ctl.lit == {})

    note = ctl.mouse_down(board.rect(64).center_x, board.height - 0.001)
    check("pressing the mouse on a key plays it, hard at the front edge",
          note == 64 and played == [(64, 127)] and ctl.mouse_note == 64,
          str(played))
    ctl.mouse_dragged(board.rect(64).center_x + 0.5, board.height - 0.001)
    check("wiggling within one key does not re-trigger it", len(played) == 1)
    ctl.mouse_dragged(board.rect(66).center_x, board.black_height * 0.5)
    check("dragging onto the next key is a glissando, not a chord",
          played[-1][0] == 66 and stopped == [64] and ctl.mouse_note == 66,
          f"played={played[-1]} stopped={stopped}")
    ctl.mouse_dragged(board.width + 50.0, deep)
    check("dragging off the edge keeps holding the note it was playing",
          ctl.mouse_note == 66 and stopped == [64])
    ctl.mouse_up()
    check("letting go stops exactly that note", stopped == [64, 66]
          and ctl.mouse_note is None)
    ctl.mouse_up()
    check("letting go twice does not send a second note-off", stopped == [64, 66])
    ctl.mouse_dragged(board.rect(60).center_x, deep)
    check("dragging with no button down plays nothing", len(played) == 2)
    check("clicking off the keyboard plays nothing and releases what was held",
          ctl.mouse_down(-5.0, 5.0) is None and len(played) == 2
          and ctl.mouse_note is None)
    top_note = ctl.mouse_down(board.rect(64).center_x, 1.0)
    check("clicking at the top of a key is much softer than at its front edge",
          top_note == 64 and played[-1][1] < 127 // 2, f"velocity {played[-1][1]}")
    ctl.mouse_up()
    ctl.settings_clicked()
    check("the gear hands straight over to the tray's menu", gear == [1])
    ctl.lights.press(64, 64)
    ctl.refresh()
    check("the controller paints from its own snapshot of the lights",
          {op.note: op.fill for op in ctl.draw_plan() if op.kind == "key"}[64]
          == kb.lit_color(64, 64))


# --------------------------------------------------------------------- mute
def max_step(block) -> float:
    """Largest jump between two neighbouring samples, over both channels.

    This is what a click *is*: a discontinuity in the waveform. Measuring it on
    the rendered samples (rather than on the gain the code believes it applied)
    is the only assertion that can tell a real fade from a plausible-looking one.
    """
    return float(np.abs(np.diff(block, axis=0)).max()) if block.shape[0] > 1 else 0.0


def edge_step(before, after) -> float:
    """The jump across the seam between two consecutively rendered blocks."""
    return float(np.abs(after[0] - before[-1]).max())


# ------------------------------------------------------ "immediate" silences
#: Largest jump, in full-scale units, that any of the immediate cuts below may
#: leave in the waveform. Two orders of magnitude under the 0.343 step dropping
#: the voice list used to leave at volume 1.0 (0.92 at 1.5 -- a full-scale edge),
#: and below the slew the ringing chord under test makes on its own, so it is a
#: bound on the *cut* and not on the music.
DECLICK_STEP_LIMIT = 0.01

#: How close to the ringing chord's own peak the sample before the cut has to
#: sit before `immediate_cut` will measure a seam against it, and how many
#: single frames it may advance looking for one. A quarter of the peak is
#: comfortably clear of a zero crossing; 400 frames is under 10 ms, far less
#: than a cycle of the lowest note in any chord these tests strike.
_SEAM_FLOOR = 0.25
_SEAM_SEARCH = 400


class CutReport:
    """What one immediate cut did to the samples; see `immediate_cut`."""

    __slots__ = ("engine", "before", "block", "after", "voices_before",
                 "voices_after", "seam", "worst_step", "playing_step",
                 "hard_seam", "env", "env_err", "decays", "tail_peak",
                 "head_ratio")


def immediate_cut(build, cut, frames: int = 1024, warm: int = 60) -> CutReport:
    """Cut a ringing engine "immediately" and measure what reaches the device.

    `build()` returns a freshly struck engine; *two* identical ones are built and
    warmed for the same number of blocks, and only one of them is cut. Sample
    playback is deterministic, so the twin's next block is exactly the audio the
    cut engine would have gone on producing -- which makes the release envelope
    the cut applied readable as a per-sample ratio (`env`) instead of something
    that has to be guessed at from a waveform which is oscillating anyway. That
    is the only way to say "monotonically decaying" about a block of music.

    `hard_seam` is what the defect this replaced measured: dropping the voice
    list made the next block all zeros, so the step it left at the boundary was
    exactly the amplitude of the last sample rendered before it.

    The chord is left ringing for `warm` blocks first, so the hammer transient --
    whose own sample-to-sample slew is 0.13 -- is over and the step the cut
    leaves is measured against a signal that is not stepping by itself.
    """
    m = CutReport()
    m.engine, twin = build(), build()
    for _ in range(warm):
        m.before = m.engine.render(frames)
        free = twin.render(frames)
    # Land the block boundary somewhere the chord is actually moving. `hard_seam`
    # below is one sample -- the last one before the cut -- and it is the whole
    # basis for claiming a hard cut would have clicked. Four notes ringing for a
    # second and a half sum to whatever they sum to at that instant, so on some
    # tunings it lands near a zero crossing and the *precondition* fails while
    # nothing about the declick has changed. (It did: stretch tuning moved the
    # bass a cent and took `hard_seam` from 0.09 to 0.015.) Nudging the boundary
    # forward a few samples until it lands on a peak costs nothing, is as
    # deterministic as the rest of this, and stops the check being a hostage to
    # the tone model.
    for _ in range(_SEAM_SEARCH):
        peak = float(np.abs(m.before).max())
        if abs(float(np.abs(m.before[-1]).max())) >= _SEAM_FLOOR * peak:
            break
        m.before = np.concatenate((m.before[1:], m.engine.render(1)))
        free = np.concatenate((free[1:], twin.render(1)))
    if not np.array_equal(m.before, free):      # pragma: no cover - determinism
        raise AssertionError("the twin engines diverged; the ratio below is void")
    m.voices_before = m.engine.active_voices
    cut(m.engine)
    # Read *before* anything is rendered: "immediate" is a promise to the caller,
    # not to the audio thread.
    m.voices_after = m.engine.active_voices
    m.block = m.engine.render(frames)
    free = twin.render(frames)
    m.after = m.engine.render(frames)

    m.seam = edge_step(m.before, m.block)
    m.worst_step = max_step(m.block)
    m.playing_step = max_step(m.before)
    m.hard_seam = float(np.abs(m.before[-1]).max())
    m.tail_peak = float(np.abs(m.after).max())
    m.head_ratio = (float(np.abs(m.block[:16]).max())
                    / max(float(np.abs(m.before[-16:]).max()), 1e-12))
    # The gain the cut applied, sample by sample, wherever the un-cut twin is
    # loud enough for the division to mean anything.
    live = np.abs(free).max(axis=1) > 1e-3
    m.env = (np.abs(m.block).max(axis=1)[live]
             / np.abs(free).max(axis=1)[live])
    ideal = np.exp(-(np.nonzero(live)[0] + 1.0)
                   / (synth.DECLICK_TAU * m.engine.samplerate))
    m.env_err = float(np.abs(m.env - ideal).max()) if m.env.size else 1.0
    m.decays = bool(m.env.size > 32 and np.all(np.diff(m.env) <= 1e-6))
    return m


def declicked(m: CutReport, limit: float = DECLICK_STEP_LIMIT):
    """(ok, detail) for the block an immediate cut hands to the device.

    Everything an "immediate" silence has to be at once: no voice left for a key
    or a pedal to reach, no step in the waveform, a monotonically decaying tail
    that really is the `DECLICK_TAU` exponential, and exact silence from the next
    block on. The `hard_seam` clause keeps the whole thing honest: if the chord
    were too quiet for a hard cut to click, none of the above would prove
    anything.
    """
    ok = (m.voices_after == 0
          and m.seam <= limit and m.worst_step <= limit
          and m.decays and float(m.env[0]) > 0.95 and float(m.env[-1]) < 1e-3
          and m.env_err < 0.05
          and m.head_ratio > 0.3
          and m.tail_peak == 0.0
          and m.hard_seam > 5.0 * limit)
    return ok, (f"{m.voices_before}->{m.voices_after} voices, seam {m.seam:.5f} "
                f"(hard cut {m.hard_seam:.3f}), worst step {m.worst_step:.5f} "
                f"vs {m.playing_step:.5f} playing, env {float(m.env[0]):.4f}.."
                f"{float(m.env[-1]):.1e} monotonic={m.decays} err={m.env_err:.4f}, "
                f"next block peak {m.tail_peak:.1e}")


def test_mute(bank: SampleBank) -> None:
    """The output-stage mute: the ramp, the voice lifecycle and the handover.

    Everything here is the audio thread's own logic, so all of it is checked on
    rendered samples: `tools/tray_smoke.py` owns the UI side (the three toggles,
    the icon, recording while muted).
    """
    print("\n[9] mute")

    def chord_engine(preset: str = "off", blocksize: int = 64) -> AudioEngine:
        eng = AudioEngine(bank, blocksize=blocksize, volume=1.0,
                          reverb_preset=preset)
        for note in (40, 47, 52, 55):     # long bass buffers: 5.7-8.0 s
            eng.note_on(note, 100)
        return eng

    # ---- the handover, exactly the one set_reverb() uses.
    eng = chord_engine()
    eng.set_muted(True)
    check("set_muted publishes the state at once but only queues the gain",
          eng.muted is True and eng._pending_mute is True
          and eng.mute_gain == 1.0, f"pending={eng._pending_mute}")
    eng.render(64)
    check("the audio thread consumes the request and starts fading",
          eng._pending_mute is None and 0.0 < eng.mute_gain < 1.0,
          f"gain={eng.mute_gain:.4f}")
    coalesce = chord_engine()
    coalesce.set_muted(True)
    coalesce.set_muted(False)
    coalesce.render(64)
    check("two toggles before one block leave the last one standing",
          coalesce.muted is False and coalesce.mute_gain == 1.0
          and coalesce._mute_target == 1.0, f"gain={coalesce.mute_gain}")

    # ---- the ramp itself, on the samples.
    RAMP_FRAMES = int(round(synth.MUTE_RAMP * tone.SAMPLE_RATE))
    check("the ramp is long enough to be inaudible and short enough to feel "
          "instant", 0.003 <= synth.MUTE_RAMP <= 0.030,
          f"{synth.MUTE_RAMP * 1000:.1f} ms = {RAMP_FRAMES} frames")

    ramped = chord_engine()
    warm = np.concatenate([ramped.render(64) for _ in range(24)])
    playing = max_step(warm)
    check("the chord under test is loud enough for a cut to be audible",
          float(np.abs(warm[-64:]).max()) > 0.05 and playing > 0.0,
          f"peak={float(np.abs(warm[-64:]).max()):.3f} step={playing:.5f}")

    # The same chord, muted twice: once with the real ramp, once with a ramp one
    # frame long -- which is a hard cut, and is the discontinuity the fade exists
    # to remove. Both engines render the identical samples, so the comparison is
    # between two measurements rather than against a guessed threshold.
    cut = chord_engine()
    for _ in range(24):
        cut.render(64)
    real_ramp = synth.MUTE_RAMP
    synth.MUTE_RAMP = 1.0 / tone.SAMPLE_RATE
    try:
        cut.set_muted(True)
        cut_blocks = np.concatenate([cut.render(64) for _ in range(4)])
    finally:
        synth.MUTE_RAMP = real_ramp
    hard = max_step(np.concatenate([warm[-64:], cut_blocks]))

    ramped.set_muted(True)
    fade = [ramped.render(64) for _ in range(RAMP_FRAMES // 64 + 3)]
    faded = np.concatenate(fade)
    across = max_step(np.concatenate([warm, faded]))
    check("muting a ringing chord is a ramp, not a step",
          across <= playing * 1.5,
          f"largest jump {across:.5f} vs {playing:.5f} while playing")
    # The two engines are at the same point in the same waveform, so the seam a
    # hard cut leaves there is measured rather than assumed: it is the last
    # sample's own amplitude, and the ramp has to be a fraction of it.
    check("...and the ramp is what removes the jump a hard cut would make",
          edge_step(warm, faded) < edge_step(warm, cut_blocks) * 0.25,
          f"{edge_step(warm, faded):.5f} ramped vs "
          f"{edge_step(warm, cut_blocks):.5f} cut")
    check("a hard cut really would have been audible (so the above can fail)",
          edge_step(warm, cut_blocks) > playing and hard > playing,
          f"seam {edge_step(warm, cut_blocks):.5f} vs {playing:.5f} while playing")
    check("the fade reaches exact silence and stays there",
          ramped.mute_gain == 0.0 and float(np.abs(faded[-64:]).max()) == 0.0
          and float(np.abs(ramped.render(1024)).max()) == 0.0,
          f"gain={ramped.mute_gain}")
    fell = [float(np.abs(b).max()) for b in fade[:RAMP_FRAMES // 64]]
    check("the fade only ever goes down", all(a >= b for a, b in zip(fell, fell[1:])),
          " ".join(f"{v:.3f}" for v in fell))

    # ---- unmuting mid-fade: the voices are still there, so this is the ramp
    # joining back up rather than silence being replaced by silence.
    back = chord_engine()
    warm_b = np.concatenate([back.render(64) for _ in range(24)])
    back.set_muted(True)
    dipped = [back.render(64) for _ in range(2)]
    check("mid-fade the chord is quieter but still sounding",
          0.0 < back.mute_gain < 1.0 and back.active_voices == 4,
          f"gain={back.mute_gain:.3f}, {back.active_voices} voices")
    back.set_muted(False)
    risen = [back.render(64) for _ in range(RAMP_FRAMES // 64 + 3)]
    whole = np.concatenate([warm_b] + dipped + risen)
    check("unmuting mid-fade ramps back up without a jump either",
          max_step(whole) <= max_step(warm_b) * 1.5 and back.mute_gain == 1.0
          and back.active_voices == 4,
          f"largest jump {max_step(whole):.5f} vs {max_step(warm_b):.5f}")

    # ---- the voice lifecycle: keys held, damper down, all the way through.
    held = chord_engine(preset="hall")
    held.control_change(64, 127)          # damper
    held.control_change(66, 127)          # sostenuto, latched on the held notes
    held.control_change(67, 127)          # una corda
    for _ in range(24):
        held.render(64)
    check("precondition: four voices are held under all three pedals",
          held.active_voices == 4 and held.sustain and held.sostenuto_down
          and held.soft_pedal, f"{held.active_voices} voices")
    held.set_muted(True)
    for _ in range(RAMP_FRAMES // 64 + 4):
        held.render(64)
    check("once the fade is over the voices are dropped, not left summing",
          held.active_voices == 0 and held._by_note == {}
          and held.mute_gain == 0.0, f"{held.active_voices} voices")
    check("dropping them does not invert the pedals the hardware is holding",
          held.sustain and held.sostenuto_down and held.soft_pedal)
    check("the reverb tail does not outlive the fade",
          float(np.abs(np.concatenate(
              [c.buf for c in held.reverb._combs_l + held.reverb._combs_r])).max())
          == 0.0)

    # Playing on while muted: the MIDI still arrives (the tray keeps recording
    # and lighting keys from it), but no voice may be allocated for it.
    held.note_on(60, 110)
    held.note_on(64, 110)
    check("note_on while muted allocates nothing",
          held.active_voices == 0 and held._by_note == {},
          f"{held.active_voices} voices")
    reverb_calls = []
    real_process = held.reverb.process
    held.reverb.process = lambda block: reverb_calls.append(block.shape) or real_process(block)
    silent = np.concatenate([held.render(64) for _ in range(8)])
    check("a muted block is not synthesised, filtered or gained",
          float(np.abs(silent).max()) == 0.0 and not reverb_calls,
          f"{len(reverb_calls)} reverb calls")

    # Letting go of everything while muted must not strand anything either: the
    # note-offs and the pedal-ups arrive for voices that are already gone.
    for note in (40, 47, 52, 55, 60, 64):
        held.note_off(note)
    held.control_change(64, 0)
    held.control_change(66, 0)
    held.control_change(67, 0)
    check("releasing keys and pedals while muted is a clean no-op",
          held.active_voices == 0 and held._by_note == {}
          and not held.sustain and not held.sostenuto_down
          and not held.soft_pedal)

    held.set_muted(False)
    revived = np.concatenate([held.render(64) for _ in range(RAMP_FRAMES // 64 + 8)])
    held.reverb.process = real_process
    check("unmuting resurrects nothing: no voice, no tail, no click",
          held.active_voices == 0 and float(np.abs(revived).max()) == 0.0
          and held.mute_gain == 1.0 and np.isfinite(revived).all(),
          f"{held.active_voices} voices, peak={float(np.abs(revived).max()):.2e}")
    held.note_on(60, 100)
    fresh = np.concatenate([held.render(64) for _ in range(16)])
    check("and the very next note plays normally",
          held.active_voices == 1 and rms(fresh) > 1e-4,
          f"rms={rms(fresh):.4f}")
    held.note_off(60)
    for _ in range(60):
        held.render(1024)
    check("that note releases like any other - no latch left behind",
          held.active_voices == 0, f"{held.active_voices} voices")

    # ---- the ghost tail: muting while the tank rings and no voice is left ---
    # The defect this covers: the tank was only cleared when the fade dropped
    # *voices*, so the very common case -- the keys are already up, or CC120 has
    # already run, and all that is still sounding is the reverb -- left the delay
    # lines frozen mid-tail. Unmuting inside the tail bound then recirculated
    # them: a measured 0.197 peak (-14.1 dBFS) out of an app the user muted.
    def tank_energy(eng: AudioEngine) -> float:
        lines = (eng.reverb._combs_l + eng.reverb._combs_r
                 + eng.reverb._aps_l + eng.reverb._aps_r)
        return float(np.abs(np.concatenate([l.buf for l in lines])).max())

    def ringing(route: str) -> AudioEngine:
        """A hall engine whose voices are gone but whose tank is still loud."""
        eng = chord_engine(preset="hall", blocksize=256)
        for _ in range(80):                    # ~0.46 s: pump the tank
            eng.render(256)
        if route == "keys released":
            for note in (40, 47, 52, 55):
                eng.note_off(note)
        else:
            eng.control_change(120, 0)         # All Sound Off
        for _ in range(600):        # exactly as long as the voices take, no more
            eng.render(256)
            if eng.active_voices == 0 and not eng._voices:
                break               # CC120: 3 blocks. Keys up: 235.
        return eng

    for route in ("keys released", "CC120"):
        for seconds in (0.5, 3.0):
            # Two engines built the same way, so what the ghost *would* have been
            # is measured on this very tail rather than assumed: the twin is left
            # unmuted and simply plays the tail out.
            twin = ringing(route)
            audible = float(np.abs(np.concatenate(
                [twin.render(256) for _ in range(8)])).max())
            eng = ringing(route)
            loud = tank_energy(eng)
            # -46 dBFS is not a rounding error; the ghost that was measured on the
            # real app was 0.197, i.e. -14.1 dBFS.
            check(f"precondition: {route}, no voice left but the tank is loud",
                  eng.active_voices == 0 and not eng._voices and loud > 0.0
                  and audible > 0.005,
                  f"tank {loud:.4f}, {audible:.4f} "
                  f"({20 * np.log10(max(audible, 1e-12)):.1f} dBFS) still "
                  f"coming out, {eng.active_voices} voices")
            eng.set_muted(True)
            for _ in range(RAMP_FRAMES // 256 + 4):
                eng.render(256)
            check(f"...and muting clears it on the transition ({route})",
                  eng.mute_gain == 0.0 and tank_energy(eng) == 0.0,
                  f"tank {tank_energy(eng):.2e} after the fade")
            for _ in range(int(seconds * 44100) // 256):
                eng.render(256)
            eng.set_muted(False)
            out = np.concatenate([eng.render(256) for _ in range(40)])
            peak = float(np.abs(out).max())
            check(f"...so {seconds:.1f} s later unmuting replays no ghost "
                  f"({route})",
                  peak == 0.0 and eng.active_voices == 0
                  and np.isfinite(out).all(),
                  f"peak {peak:.2e} vs the {audible:.3f} that was ringing")
            eng.note_on(60, 100)
            fresh = np.concatenate([eng.render(256) for _ in range(8)])
            check(f"...and the reverb still works afterwards ({route})",
                  rms(fresh) > 1e-4 and tank_energy(eng) > 0.0,
                  f"rms {rms(fresh):.4f}, tank {tank_energy(eng):.4f}")

    # The same transition with the keys *still down* -- the case that always
    # worked -- must keep working: the tail goes, the voices go, and the pedal
    # latches do not move (that is `held` above, re-checked here on the hall
    # preset the ghost was found in).
    still_down = chord_engine(preset="hall", blocksize=256)
    still_down.control_change(64, 127)
    for _ in range(80):
        still_down.render(256)
    check("precondition: keys held, four voices, a ringing hall",
          still_down.active_voices == 4 and tank_energy(still_down) > 0.0,
          f"{still_down.active_voices} voices, tank {tank_energy(still_down):.4f}")
    still_down.set_muted(True)
    for _ in range(RAMP_FRAMES // 256 + 4):
        still_down.render(256)
    check("muting with the keys down still drops the voices and the tail",
          still_down.active_voices == 0 and tank_energy(still_down) == 0.0
          and still_down.sustain,
          f"{still_down.active_voices} voices, tank {tank_energy(still_down):.2e}")
    for _ in range(400):
        still_down.render(256)
    still_down.set_muted(False)
    check("...and unmuting is silent there too",
          float(np.abs(np.concatenate(
              [still_down.render(256) for _ in range(40)])).max()) == 0.0)

    # Muting an idle engine, and muting one that is already muted: neither may
    # leave the gain stranded halfway or the voices half-dropped.
    idle = AudioEngine(bank, blocksize=64, volume=1.0, reverb_preset="off")
    idle.set_muted(True)
    idle.set_muted(True)
    for _ in range(RAMP_FRAMES // 64 + 4):
        idle.render(64)
    idle.set_muted(True)
    check("muting twice, or muting silence, settles at exactly zero",
          idle.mute_gain == 0.0 and float(np.abs(idle.render(64)).max()) == 0.0)
    idle.set_muted(False)
    for _ in range(RAMP_FRAMES // 64 + 4):
        idle.render(64)
    check("and unmuting always comes all the way back to unity",
          idle.mute_gain == 1.0 and idle.muted is False)

    # The stream is deliberately left open: unmuting must not have to re-acquire
    # a device (and cannot fail).
    check("mute never touches the audio stream",
          idle.stream is None and _raises(idle.set_muted, True) is None
          and _raises(idle.set_muted, False) is None)


# -------------------------------------------------------------- mute hotkey
def test_hotkey() -> None:
    """Spec parsing, event matching and the monitor's dispatch logic.

    All pure Python: the two NSEvent monitors are stood up against a faithful
    NSEvent stub in `tools/tray_smoke.py`, but everything that decides *whether*
    a key press is the hotkey lives here, where it can be checked exhaustively.
    """
    print("\n[10] mute hotkey")
    from justpiano import hotkey
    from justpiano.config import DEFAULTS

    check("the parser needs no AppKit to decide what a hotkey is",
          "AppKit" not in sys.modules and "objc" not in sys.modules)
    check("without pyobjc there are no monitors and no crash",
          hotkey.available() is False and hotkey.trusted() is None)

    default = hotkey.parse(DEFAULTS["mute_hotkey"])
    check("the default hotkey parses to a physical key and its modifiers",
          default is not None and default.key_code == hotkey.KEY_CODES["m"]
          and default.modifiers == (hotkey.CONTROL | hotkey.OPTION
                                    | hotkey.COMMAND), repr(default))
    check("the default is deliberately awkward enough to miss a DAW's shortcuts",
          bin(default.modifiers).count("1") >= 3
          and default.label == "⌃⌥⌘M", default.label)
    check("the persisted default is stored in its own canonical form",
          default.spec == DEFAULTS["mute_hotkey"], repr(default.spec))

    fkey = hotkey.parse("f13")
    check("a bare function key needs no modifiers at all",
          fkey is not None and fkey.key_code == 105 and fkey.modifiers == 0
          and fkey.label == "F13", repr(fkey))
    check("specs are normalised, so what is written back is canonical",
          hotkey.parse(" CMD + Ctrl + M ").spec == "ctrl+cmd+m"
          and hotkey.parse("⌃+⌥+⌘+m").spec == default.spec,
          repr(hotkey.parse(" CMD + Ctrl + M ").spec))
    # settings.json is a file a user can edit: nonsense in it must cost the
    # hotkey, never the launch.
    for spec in (None, "", "+", "ctrl+", "hyper+m", "ctrl+alt+nope", 42, ["m"]):
        check(f"a spec that is not a hotkey is simply not one: {spec!r}",
              hotkey.parse(spec) is None and hotkey.label(spec) is None)

    # Matching. The four modifiers are exact; the ones the keyboard sets by
    # itself are ignored, or an F-key (macOS always reports fn) and anyone with
    # caps lock on would never match.
    check("the hotkey matches its own key and modifiers",
          default.matches(hotkey.KEY_CODES["m"],
                          hotkey.CONTROL | hotkey.OPTION | hotkey.COMMAND))
    check("caps lock, fn and the numeric-pad bit are not part of a shortcut",
          default.matches(hotkey.KEY_CODES["m"],
                          hotkey.CONTROL | hotkey.OPTION | hotkey.COMMAND
                          | (1 << 16) | hotkey.FUNCTION | (1 << 21))
          and fkey.matches(105, hotkey.FUNCTION))
    check("a superset of the modifiers is somebody else's shortcut",
          not default.matches(hotkey.KEY_CODES["m"],
                              hotkey.CONTROL | hotkey.OPTION | hotkey.COMMAND
                              | hotkey.SHIFT))
    check("so is a subset, and so is the wrong key",
          not default.matches(hotkey.KEY_CODES["m"], hotkey.COMMAND)
          and not default.matches(hotkey.KEY_CODES["n"],
                                  hotkey.CONTROL | hotkey.OPTION | hotkey.COMMAND)
          and not fkey.matches(105, hotkey.COMMAND))
    check("junk out of an event does not raise out of the handler",
          default.matches(None, 0) is False and default.matches(46, "⌘") is False)

    # The monitor's dispatch, driven by hand. A key-down event is the only thing
    # it reads, so it can be a plain object here.
    class _Event:
        def __init__(self, key_code, flags, repeat=False):
            self._k, self._f, self._r = key_code, flags, repeat

        def keyCode(self):
            return self._k

        def modifierFlags(self):
            return self._f

        def isARepeat(self):
            return self._r

    fired = []
    monitor = hotkey.HotkeyMonitor(lambda: fired.append(1))
    armed = monitor.apply(DEFAULTS["mute_hotkey"])
    check("arming without AppKit still parses but installs nothing",
          armed == default and monitor.hotkey == default
          and monitor.installed is False and not monitor.local_installed
          and not monitor.global_installed)
    mods = hotkey.CONTROL | hotkey.OPTION | hotkey.COMMAND
    check("the hotkey fires once per press",
          monitor._handle(_Event(46, mods)) is True and fired == [1]
          and monitor.triggers == 1)
    check("key repeat does not toggle mute at the repeat rate",
          monitor._handle(_Event(46, mods, repeat=True)) is False
          and monitor.triggers == 1)
    check("another shortcut passing by does nothing",
          monitor._handle(_Event(46, hotkey.COMMAND)) is False
          and monitor._handle(_Event(45, mods)) is False
          and monitor.triggers == 1)
    check("the local monitor never swallows the key it saw",
          monitor._local_handler(_Event(46, mods)) is not None
          and monitor.triggers == 2)
    check("an event that cannot be read is ignored, not raised",
          monitor._handle(object()) is False and monitor.triggers == 2)

    boom = hotkey.HotkeyMonitor(lambda: 1 / 0)
    check("a callback that explodes does not unwind into ObjC",
          boom.apply("f13") is not None
          and _raises(boom._handle, _Event(105, hotkey.FUNCTION)) is None
          and boom.triggers == 1)

    monitor.apply(None)
    check("disarming leaves nothing armed and nothing listening",
          monitor.hotkey is None and monitor.installed is False
          and monitor._handle(_Event(46, mods)) is False and fired == [1, 1])
    check("stopping a monitor that was never installed is a no-op",
          _raises(monitor.stop) is None and monitor.installed is False)


# --------------------------------------------------------------- menu bar art
def test_icons() -> None:
    """The generated images, checked as pixels rather than by eye.

    The muted menu bar icon has to be a *template* image - black with an alpha
    mask - or macOS will not recolour it for a dark menu bar, and the slash has
    to survive being drawn on either background, which is what the transparent
    gutter around it is for.
    """
    print("\n[11] menu bar art")
    from tools import make_icon

    art = make_icon.render_muted()
    alpha = art[..., 3]
    check("the muted icon is a Retina menu bar image",
          art.shape == (make_icon.MENUBAR_SIZE, make_icon.MENUBAR_SIZE, 4)
          and art.dtype == np.uint8 and make_icon.MENUBAR_SIZE / 2 == 22.0,
          str(art.shape))
    check("it is a template image: black everywhere, shape carried by the alpha",
          int(art[..., :3].max()) == 0 and int(alpha.max()) == 255,
          f"rgb max {int(art[..., :3].max())}, alpha max {int(alpha.max())}")
    check("it is a glyph, not a blob or a smear",
          0.15 < float(alpha.mean()) / 255.0 < 0.60
          and int(alpha[0, 0]) == 0 and int(alpha[-1, -1]) == 0,
          f"coverage {float(alpha.mean()) / 255.0:.0%}")
    check("it is anti-aliased rather than hard-edged",
          int(((alpha > 8) & (alpha < 247)).sum()) > make_icon.MENUBAR_SIZE,
          f"{int(((alpha > 8) & (alpha < 247)).sum())} partial pixels")

    # Down the middle: keybed, gutter, slash, gutter, keybed. Without the gutter
    # the slash would vanish into the keys on one of the two menu bar themes.
    row = alpha[make_icon.MENUBAR_SIZE // 2] > 128
    runs: list[bool] = []
    for inked in row:
        if not runs or runs[-1] != bool(inked):
            runs.append(bool(inked))
    check("the slash is kept off the keys by a transparent gutter",
          runs.count(True) >= 2 and runs.count(False) >= 3, str(runs))

    small = make_icon.render_muted(size=12, ss=3)
    check("the renderer is parametrised, not nailed to one size",
          small.shape == (12, 12, 4) and int(small[..., 3].max()) > 0)

    # Low down the keybed the naturals have to read as separate keys, or the
    # glyph is just a slashed rectangle: the seams are cut to the same
    # proportions the 1024 px app icon uses (KEYBED_* are shared by both).
    band = alpha[int(make_icon.MENUBAR_SIZE * 0.70)] > 128
    keys: list[bool] = []
    for inked in band:
        if not keys or keys[-1] != bool(inked):
            keys.append(bool(inked))
    check("the keybed reads as a row of naturals, not one solid block",
          make_icon.KEYBED_WHITES == 7 and keys.count(True) >= 5
          and keys.count(False) >= 5, str(keys))

    # The shipped file has to be the one this code renders: a checkout whose
    # icon-muted.png predates a change to make_icon.py would ship the old glyph.
    assets = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(make_icon.__file__))), "assets")
    path = os.path.join(assets, "icon-muted.png")
    on_disk = None
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            on_disk = fh.read()
    fresh = os.path.join(_HOME, "icon-muted.png")
    make_icon.write_png(fresh, art)
    with open(fresh, "rb") as fh:
        expected = fh.read()
    check("assets/icon-muted.png exists and is a PNG",
          on_disk is not None and on_disk[:8] == b"\x89PNG\r\n\x1a\n", path)
    check("...and is up to date with make_icon.py (else: python3 -m tools.make_icon)",
          on_disk == expected,
          f"{len(on_disk or b'')} bytes on disk vs {len(expected)} rendered")

    # ---- and the same for the *app* icon -----------------------------------
    # assets/icon.png is what build_app.sh turns into icon.icns, i.e. the Dock
    # and Finder icon of the shipped bundle, and nothing checked it at all: a
    # change to make_icon.py (SIZE, the keybed proportions, the accent bar) left
    # the old art in the bundle with the suite still green. The dimensions are
    # asserted from the PNG's own header, not from the array, so a resized
    # `render()` cannot agree with itself.
    app_path = os.path.join(assets, "icon.png")
    app_disk = None
    if os.path.isfile(app_path):
        with open(app_path, "rb") as fh:
            app_disk = fh.read()
    width = height = depth = colour = None
    if app_disk and app_disk[12:16] == b"IHDR":
        width = int.from_bytes(app_disk[16:20], "big")
        height = int.from_bytes(app_disk[20:24], "big")
        depth, colour = app_disk[24], app_disk[25]
    check("assets/icon.png is a PNG at the size the app bundle needs",
          app_disk is not None and app_disk[:8] == b"\x89PNG\r\n\x1a\n"
          and (width, height) == (make_icon.SIZE, make_icon.SIZE)
          and make_icon.SIZE == 1024 and (depth, colour) == (8, 6),
          f"{width}x{height}, {make_icon.SIZE=}, bit depth {depth}, colour type {colour}")
    app_art = make_icon.render()          # ~9 s: the 4096px supersampled pass
    app_fresh = os.path.join(_HOME, "icon.png")
    make_icon.write_png(app_fresh, app_art)
    with open(app_fresh, "rb") as fh:
        app_expected = fh.read()
    check("...and is up to date with make_icon.py (it becomes the Dock icon)",
          app_disk == app_expected
          and app_art.shape == (make_icon.SIZE, make_icon.SIZE, 4),
          f"{len(app_disk or b'')} bytes on disk vs {len(app_expected)} rendered, "
          f"render {app_art.shape}")
    check("the app icon is full-colour art, not the flat template glyph",
          int(app_art[..., :3].max()) > 200 and int(app_art[..., 3].max()) == 255
          and int(app_art[..., 3].min()) == 0
          and int(art[..., :3].max()) == 0,
          f"rgb max {int(app_art[..., :3].max())} vs muted "
          f"{int(art[..., :3].max())}")


def _reverb_run(bank, preset, seconds, idle_limit=None):
    """Play a two-note chord, cut it, and keep rendering into the silence.

    Returns `(audio, reverb_calls, total_blocks)`. With `idle_limit` forced huge
    the reverb runs on every block, which is the ungated reference the gate is
    compared against.
    """
    eng = AudioEngine(bank, blocksize=1024, volume=1.0, reverb_preset=preset)
    if idle_limit is not None:
        eng._reverb_idle_limit = idle_limit
    calls = [0]
    real = eng.reverb.process

    def counting(block):
        calls[0] += 1
        real(block)

    eng.reverb.process = counting
    eng.note_on(60, 127)
    eng.note_on(48, 127)
    out = []
    total = int(44100 * seconds / 1024)
    for i in range(total):
        if i == 10:
            eng.note_off(60)
            eng.note_off(48)
        out.append(eng.render(1024).copy())
    eng.reverb.process = real
    return np.concatenate(out), calls[0], total


def test_reverb_gate(bank: SampleBank) -> None:
    print("\n[3b] reverb idle gate")
    # Idle, with nothing playing and the shipped Room preset, `Reverb.process()`
    # cost 0.189 ms of every 256-frame callback: 3.3 % of a core, for ever, on an
    # app whose whole point is to sit in the menu bar. render() now stops calling
    # it once the tail is provably below `reverb.TAIL_FLOOR` -- but the bound has
    # to be per-preset, because Concert Hall rings two and a half times as long
    # as Room, and a guessed constant is how a real tail gets chopped off.
    room, hall = Reverb("room").tail_frames, Reverb("hall").tail_frames
    check("the tail bound is derived per preset, not guessed",
          0 == Reverb("off").tail_frames < room < hall,
          f"off=0 room={room / 44100:.1f}s hall={hall / 44100:.1f}s")
    check("the tail bound follows the comb feedback it is computed from",
          abs(REVERB_PRESETS["hall"]["roomsize"] * 0.28 + 0.70) ** (hall / LONGEST_LINE)
          <= TAIL_FLOOR * 1.000001,
          f"{hall / LONGEST_LINE:.0f} round trips")

    lsb = 1.0 / 32768.0
    for preset, seconds in (("room", 9.0), ("hall", 17.0)):
        limit = (Reverb(preset).tail_frames
                 + int(synth.REVERB_IDLE_MARGIN * 44100))
        gated, gated_calls, blocks = _reverb_run(bank, preset, seconds)
        free, free_calls, _ = _reverb_run(bank, preset, seconds, idle_limit=1 << 40)
        check(f"the {preset} reverb stops being processed on sustained silence",
              free_calls == blocks and gated_calls < blocks * 0.95,
              f"{gated_calls}/{blocks} blocks processed (ungated {free_calls})")
        # ...and not one sample before it is safe. Everything up to the bound is
        # bit-identical to the render that never gates.
        check(f"a live {preset} tail is bit-identical to an ungated render",
              np.array_equal(gated[:limit], free[:limit]),
              f"first {limit / 44100:.1f}s")
        # What the gate does discard has to be below what the output format can
        # even carry: 16-bit audio has no number smaller than 1/32768.
        differs = np.nonzero(np.abs(gated - free).max(axis=1))[0]
        residual = float(np.abs(free[differs[0]:]).max()) if differs.size else 0.0
        check(f"what the {preset} gate drops is unrepresentable in int16",
              residual < lsb * 1e-3,
              f"peak {residual:.2e} = {residual / lsb:.1e} of an int16 LSB, "
              f"from {differs[0] / 44100:.1f}s" if differs.size else "nothing dropped")

    # A gate that fired while a note was still sounding would be audible at once,
    # so the counter has to be reset by the voices and not by the clock.
    eng = AudioEngine(bank, blocksize=1024, volume=1.0, reverb_preset="hall")
    for _ in range(int(44100 * 20 / 1024)):
        eng.render(1024)                       # settle the gate on silence
    settled = eng._idle_frames
    eng.note_on(60, 110)
    eng.render(1024)
    check("a note re-arms the gate immediately",
          settled > eng._reverb_idle_limit and eng._idle_frames == 0,
          f"{settled} -> {eng._idle_frames} idle frames")
    check("...and the reverb is audible again on the very next block",
          rms(eng.render(1024)) > 1e-4)

    # ---- the cushion on top of the bound -----------------------------------
    # `tail_frames` is a bound on the *tank*, but `_idle_frames` is counted in
    # whole blocks by the mixer, and it starts the moment the voice list is
    # empty -- which is not the moment the last sample the voices produced has
    # finished circulating through the CHUNK loop. `REVERB_IDLE_MARGIN` is the
    # slack that covers the difference; at 0.0 the gate lands within a few
    # hundred frames of the theoretical bound, with nothing between the two.
    from justpiano.config import MAX_BLOCKSIZE
    margin_frames = int(synth.REVERB_IDLE_MARGIN * 44100)
    gate = AudioEngine(bank, blocksize=1024, volume=1.0, reverb_preset="room")
    check("the idle bound is the tail bound plus a cushion, not the bound itself",
          gate._reverb_idle_limit == gate.reverb.tail_frames + margin_frames
          and 0.05 <= synth.REVERB_IDLE_MARGIN <= 1.0
          and margin_frames >= MAX_BLOCKSIZE,
          f"{gate.reverb.tail_frames} + {margin_frames} frames "
          f"({synth.REVERB_IDLE_MARGIN * 1e3:.0f} ms) at a "
          f"{MAX_BLOCKSIZE}-frame largest block")
    # ...and it is a cushion in practice, not just in the arithmetic: run one
    # engine right up to the block on which it stops processing, and see how far
    # past the bound that is. The tank is measured there too -- what the gate
    # throws away has to be nothing at all.
    gate.note_on(60, 127)
    gate.note_on(48, 127)
    tank_at_gate = None
    for i in range(2000):
        if i == 10:
            gate.note_off(60)
            gate.note_off(48)
        lines = gate.reverb._combs_l + gate.reverb._combs_r
        before = float(np.abs(np.concatenate([c.buf for c in lines])).max())
        gate.render(1024)
        if gate._reverb_settled:
            tank_at_gate = before
            break
    over = gate._idle_frames - gate.reverb.tail_frames
    check("the gate fires a full block's worth of slack past the tail bound",
          tank_at_gate is not None and over >= MAX_BLOCKSIZE
          and tank_at_gate < TAIL_FLOOR,
          f"{over} frames past the bound ({over / 44100 * 1e3:.0f} ms), "
          f"tank {tank_at_gate:.2e} vs a {TAIL_FLOOR:.0e} floor")


def test_footprint(bank: SampleBank) -> None:
    print("\n[3c] resident footprint and disk cache")
    # ---- 1. the bank is mapped, not read into the heap --------------------
    # np.load() without mmap_mode made 44.6 MiB of *dirty anonymous* RAM: memory
    # the OS can neither drop nor share, held for the entire life of a menu bar
    # app. Mapped, the same bytes are clean file-backed pages, and a session that
    # only plays two octaves faults in 12 of the 44.6 MiB.
    buf = bank.get(60, "hard")
    root = buf                       # np.memmap -> mmap.mmap: stop at the array
    while isinstance(getattr(root, "base", None), np.ndarray):
        root = root.base
    check("the sample bank is memory-mapped from its cache file",
          isinstance(root, np.memmap) and bank.mapped
          and os.path.samefile(root.filename, bank._blob_path),
          f"{type(root).__name__} {getattr(root, 'filename', None)}")
    check("...so no page of it can ever be dirtied",
          not buf.flags.writeable and not root.flags.writeable)
    check("...the slices handed to the mixer stay contiguous",
          buf.flags.c_contiguous and bank.get(60, "soft").flags.c_contiguous)
    # The `.view(np.ndarray)` is load-bearing: a raw np.memmap slice costs 0.604
    # ms per 30-voice block against 0.340 for the view, because every read goes
    # through the subclass.
    check("...and are plain ndarrays, not np.memmap subclasses",
          type(buf) is np.ndarray, type(buf).__name__)
    # A write anywhere in the mixer would turn the mapping into private dirty
    # pages (or, read-only, raise) -- so prove the mixer never writes. Hashed
    # over the whole blob, not one buffer: a scribble on any layer counts.
    before = hashlib.sha256(np.ascontiguousarray(bank._blob).tobytes()).hexdigest()
    probe = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="room")
    for note in range(55, 70):
        probe.note_on(note, 100)
    for _ in range(20):
        probe.render(256)
    check("rendering never writes to sample memory",
          hashlib.sha256(np.ascontiguousarray(bank._blob).tobytes()).hexdigest()
          == before)

    # ---- 2. the build pool is capped --------------------------------------
    # Eight workers were 33 % slower in wall clock than three (1.98 s vs 1.43),
    # burned 6.6 s of CPU against 3.4 fighting over the GIL, and lifted the
    # build's peak RSS to 139.5 MiB from 103.2.
    asked = []

    class _SpyPool(ThreadPoolExecutor):
        def __init__(self, max_workers=None, **kw):
            asked.append(max_workers)
            super().__init__(max_workers=max_workers, **kw)

    tiny = SampleBank(samplerate=1)
    real_pool, real_render = samplebank.ThreadPoolExecutor, tone.render_note
    samplebank.ThreadPoolExecutor = _SpyPool
    tone.render_note = lambda *_a, **_kw: np.zeros(4, dtype=np.float32)
    try:
        tiny._build(None)
    finally:
        samplebank.ThreadPoolExecutor = real_pool
        tone.render_note = real_render
    check("the build thread pool is capped well below the core count",
          asked == [samplebank.MAX_BUILD_WORKERS]
          and 2 <= samplebank.MAX_BUILD_WORKERS <= 4
          and samplebank.MAX_BUILD_WORKERS < (os.cpu_count() or 4),
          f"asked for {asked}, cap {samplebank.MAX_BUILD_WORKERS}, "
          f"{os.cpu_count()} cores")

    # ---- 3. the cache is written without materialising the bank -----------
    keys = sorted(bank._data.keys())
    expected = np.concatenate([np.ascontiguousarray(bank._data[k]) for k in keys])
    tracemalloc.start()
    try:
        bank._save_cache()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    check("writing the cache never builds the whole bank in memory",
          peak < expected.nbytes // 8,
          f"{peak / 2**20:.1f} MiB peak for a {expected.nbytes / 2**20:.1f} MiB bank")
    written = np.load(bank._blob_path)
    size = os.path.getsize(bank._blob_path)
    check("the streamed .npy is byte-exact and holds nothing but header+payload",
          np.array_equal(written, expected)
          and 0 < size - written.nbytes <= 4096 and (size - written.nbytes) % 64 == 0,
          f"{size - written.nbytes} header bytes, {written.size} samples")
    remap = SampleBank()
    check("...and the mmap path reads it back identically",
          remap._load_cache() and remap.mapped
          and np.array_equal(remap.get(60, "hard"), bank.get(60, "hard")))
    # A blob that is shorter than its header claims would map happily on some
    # platforms and then SIGBUS the audio thread on the first page past the end
    # of the file -- a signal no `except` can catch. It has to be refused here.
    with open(bank._blob_path, "rb") as fh:
        whole = fh.read()
    with open(bank._blob_path, "wb") as fh:
        fh.write(whole[: len(whole) // 2])
    chopped = SampleBank()
    refused = chopped._load_cache()
    with open(bank._blob_path, "wb") as fh:
        fh.write(whole)
    check("a truncated blob is refused at load, never mapped into a SIGBUS",
          refused is False and not chopped.mapped and SampleBank()._load_cache(),
          f"_load_cache() -> {refused}")

    # ---- 4. stale cache stems are pruned, current ones are not ------------
    # No os.remove existed anywhere in justpiano/, so every BANK_VERSION or
    # TONE_FINGERPRINT bump orphaned another ~127 MiB of blobs for good.
    cache = samplebank.CACHE_DIR
    prefix = samplebank.CACHE_PREFIX

    def plant(stem, tone_fp=TONE_FINGERPRINT, voicing="grand", rate=44100,
              index=True, blob=True):
        if blob:
            np.save(os.path.join(cache, f"{stem}.npy"), np.zeros(4, dtype=np.int16))
        if index:
            with open(os.path.join(cache, f"{stem}.json"), "w") as fh:
                json.dump({"samplerate": rate, "tone": tone_fp, "voicing": voicing,
                           "fingerprint": samplebank._bank_fingerprint(voicing),
                           "offsets": {}}, fh)
        return stem

    stale_tone = plant(f"{prefix}{samplebank.BANK_VERSION}_grand_48000",
                       tone_fp="0" * 16, rate=48000)
    old_version = plant(f"{prefix}{samplebank.BANK_VERSION - 1}_grand_44100")
    orphan_blob = plant(f"{prefix}{samplebank.BANK_VERSION}_grand_16000",
                        rate=16000, index=False)
    leftover = os.path.join(cache, f"{prefix}{samplebank.BANK_VERSION}_grand_8000.npy.tmp")
    with open(leftover, "wb") as fh:
        fh.write(b"half a file")
    other = SampleBank("felt")
    other_current = os.path.exists(other._index_path) and os.path.exists(other._blob_path)
    foreign = os.path.join(cache, "notes-from-a-user.txt")
    with open(foreign, "w") as fh:
        fh.write("not mine")

    removed = set(samplebank.prune_cache())
    gone = [s for s in (stale_tone, old_version, orphan_blob)
            if not os.path.exists(os.path.join(cache, f"{s}.npy"))]
    check("a cache stem this build can no longer load is deleted",
          len(gone) == 3 and leftover in removed and not os.path.exists(leftover),
          f"removed {len(removed)} files, {gone}")
    check("the current voicing's cache survives the prune",
          os.path.exists(bank._blob_path) and os.path.exists(bank._index_path))
    check("another voicing's *current* cache survives too "
          "(switching back must stay a file read)",
          other_current and os.path.exists(other._blob_path)
          and os.path.exists(other._index_path)
          and SampleBank("felt")._load_cache(),
          f"had felt cache={other_current}")
    check("nothing outside the bank cache is touched",
          os.path.exists(foreign) and samplebank.prune_cache() == [],
          "a second prune finds nothing left")

    # ---- 4b. surplus banks are evicted, least recently used first ----------
    # The rule above is about *correctness*: it removes what this build could
    # never read. Nothing removed what it can read perfectly well, so working
    # through the Instrument menu once left all five banks on disk -- ~198 MiB,
    # for ever, from an app that ships no audio at all. `MAX_CACHED_BANKS` is the
    # second rule.
    #
    # In a cache directory of its own, because the suite's own banks live in the
    # real one and this test is precisely about evicting banks that are still
    # perfectly good.
    real_budget = samplebank.MAX_CACHED_BANKS
    real_cache = samplebank.CACHE_DIR
    lru_dir = os.path.join(_HOME, "lru-cache")
    os.makedirs(lru_dir, exist_ok=True)
    try:
        samplebank.CACHE_DIR = lru_dir
        samplebank.MAX_CACHED_BANKS = 3
        planted = {}
        for voicing, age in (("grand", 400.0), ("upright", 300.0), ("felt", 200.0),
                             ("rhodes", 100.0), ("wurlitzer", 50.0)):
            stem = f"{prefix}{samplebank.BANK_VERSION}_{voicing}_44100"
            np.save(os.path.join(lru_dir, f"{stem}.npy"),
                    np.zeros(4, dtype=np.int16))
            with open(os.path.join(lru_dir, f"{stem}.json"), "w") as fh:
                json.dump({"samplerate": 44100, "tone": TONE_FINGERPRINT,
                           "voicing": voicing,
                           "fingerprint": samplebank._bank_fingerprint(voicing),
                           "offsets": {}}, fh)
            when = time.time() - age
            os.utime(os.path.join(lru_dir, f"{stem}.json"), (when, when))
            planted[voicing] = stem
        # The oldest of the five is the one in use: `keep` must hold it whatever
        # its age, and it still counts against the budget.
        evicted = {os.path.basename(f) for f in
                   samplebank.prune_cache(keep=(planted["grand"],))}
        left = sorted(v for v, stem in planted.items()
                      if os.path.exists(os.path.join(lru_dir, f"{stem}.json")))
        check("a surplus bank is evicted, oldest use first, and the one in use is kept",
              left == ["grand", "rhodes", "wurlitzer"]
              and f"{planted['upright']}.npy" in evicted
              and f"{planted['felt']}.json" in evicted,
              f"kept {left}, evicted {len(evicted)} files")
        check("...and a prune with nothing surplus left removes nothing",
              samplebank.prune_cache(keep=(planted["grand"],)) == [],
              str(sorted(os.listdir(lru_dir))))
    finally:
        samplebank.MAX_CACHED_BANKS = real_budget
        samplebank.CACHE_DIR = real_cache
        _shutil.rmtree(lru_dir, ignore_errors=True)

    # Loading a bank *is* using it. Without this the instrument you always come
    # back to -- loaded every launch, never rebuilt since the day it was first
    # played -- would date from that day and be the first one thrown away, while
    # one you tried once last week outlived it.
    stamped = bank._index_path
    stale_when = time.time() - 3600.0
    os.utime(stamped, (stale_when, stale_when))
    reloaded = SampleBank(bank.voicing)._load_cache()
    check("loading a cached bank counts as using it",
          reloaded is True and os.path.getmtime(stamped) > time.time() - 5.0,
          f"reload={reloaded}, "
          f"stamped {time.time() - os.path.getmtime(stamped):.1f}s ago")

    # ---- 5. prune_cache() against a save in flight -------------------------
    # The two-step publication (write .tmp, os.replace) looks exactly like the
    # orphan the pruner exists to delete, and the pruner ran on the build thread
    # of *another* voicing. It deleted the .tmp, `os.replace` then raised
    # FileNotFoundError, and `_load_or_build` swallowed it: no cache at all (so
    # every launch re-renders) and, because `_adopt_cache` never ran, 44.6 MiB of
    # dirty heap for the session instead of the mmap. Nothing said a word.
    def tiny_bank(voicing: str = "felt", rate: int = 8000) -> SampleBank:
        """A real bank, cheap: 176 four-sample buffers, saved like any other."""
        made = SampleBank(voicing, samplerate=rate)
        real_render = tone.render_note
        tone.render_note = lambda note, layer, *_a, **_kw: np.full(
            int(tone.note_duration(note, voicing) * rate), 1000, dtype=np.int16)
        try:
            made._build(None)
        finally:
            tone.render_note = real_render
        return made

    # (a) the worst instant, deterministically: the payload is on disk, the index
    # is not, and a prune runs right there. Reentrant on purpose -- this is the
    # `_WRITING` registry being tested, not the lock.
    victim = tiny_bank()
    for path in (victim._blob_path, victim._index_path):
        if os.path.exists(path):
            os.remove(path)
    fired = []
    pruned_mid: list[str] = []
    real_replace = os.replace

    def replace_and_prune(src, dst):
        if str(src).endswith(".npy.tmp") and not fired:
            fired.append(src)
            pruned_mid.extend(samplebank.prune_cache())
        return real_replace(src, dst)

    os.replace = replace_and_prune
    try:
        blew_up = _raises(victim._save_cache)
    finally:
        os.replace = real_replace
    reread = SampleBank(victim.voicing, samplerate=victim.samplerate)
    check("a prune between the payload and the rename cannot break the save",
          bool(fired) and blew_up is None
          and not any(victim._stem in p for p in pruned_mid)
          and os.path.exists(victim._blob_path)
          and _safe(reread._load_cache) is True,
          f"pruned {len(pruned_mid)} files mid-write, save raised {blew_up!r}")

    # (b) and the lock really does serialise the two, across threads: the pruner
    # is started at that same instant and must still be waiting when the writer
    # gets to its rename.
    victim_b = tiny_bank("upright", 8000)
    for path in (victim_b._blob_path, victim_b._index_path):
        if os.path.exists(path):
            os.remove(path)
    done = threading.Event()
    fired_b: list[str] = []
    state = {"blocked": None, "pruned": None}

    def replace_and_race(src, dst):
        if str(src).endswith(".npy.tmp") and not fired_b:
            fired_b.append(src)

            def prune() -> None:
                state["pruned"] = samplebank.prune_cache()
                done.set()

            threading.Thread(target=prune, name="pruner").start()
            state["blocked"] = not done.wait(0.25)   # it must not get in here
        return real_replace(src, dst)

    os.replace = replace_and_race
    try:
        raced = _raises(victim_b._save_cache)
    finally:
        os.replace = real_replace
    done.wait(10.0)
    reread_b = SampleBank(victim_b.voicing, samplerate=victim_b.samplerate)
    check("a concurrent prune waits for the whole write, not for one rename",
          bool(fired_b) and state["blocked"] is True and raced is None
          and _safe(reread_b._load_cache) is True,
          f"pruner blocked={state['blocked']} for 250 ms, then removed "
          f"{len(state['pruned'] or [])} files; save raised {raced!r}")

    # (c) hammered: saves and prunes at once, from four threads, for real.
    stop_race = threading.Event()
    race_errors: list[str] = []
    saves = [0]
    prunes = [0]
    seen_tmp = [0]

    def saver(b: SampleBank) -> None:
        while not stop_race.is_set() and saves[0] < 24:
            if not b._try_save_cache():
                race_errors.append(b.error or "?")
            saves[0] += 1

    def pruner() -> None:
        while not stop_race.is_set():
            if any(n.endswith(".tmp") for n in os.listdir(samplebank.CACHE_DIR)):
                seen_tmp[0] += 1
            prunes[0] += 1
            samplebank.prune_cache()

    hands = [threading.Thread(target=saver, args=(tiny_bank("felt", 8000),)),
             threading.Thread(target=saver, args=(tiny_bank("upright", 8000),)),
             threading.Thread(target=pruner, daemon=True),
             threading.Thread(target=pruner, daemon=True)]
    for t in hands[:2]:
        t.start()
    for t in hands[2:]:
        t.start()
    for t in hands[:2]:
        t.join(60.0)
    stop_race.set()
    for t in hands[2:]:
        t.join(10.0)
    leftovers = [n for n in os.listdir(samplebank.CACHE_DIR) if n.endswith(".tmp")]
    check("saving and pruning concurrently loses neither the cache nor a word",
          not race_errors and saves[0] >= 24 and prunes[0] > 0
          and not leftovers
          and _safe(SampleBank("felt", samplerate=8000)._load_cache) is True
          and _safe(SampleBank("upright", samplerate=8000)._load_cache) is True,
          f"{saves[0]} saves against {prunes[0]} prunes, "
          f"{len(race_errors)} errors {race_errors[:1]}, tmp left {leftovers}")

    # A `.tmp` belonging to a *live* writer is kept; the identical file with no
    # writer behind it (a crashed one) is still rubbish and still goes.
    crashed = f"{prefix}{samplebank.BANK_VERSION}_grand_7777"
    crashed_tmp = os.path.join(cache, f"{crashed}.npy.tmp")
    with open(crashed_tmp, "wb") as fh:
        fh.write(b"half a payload")
    samplebank._WRITING.add(crashed)
    try:
        kept = samplebank.prune_cache()
    finally:
        samplebank._WRITING.discard(crashed)
    survived = os.path.exists(crashed_tmp)
    after = samplebank.prune_cache()
    check("a .tmp is spared only while its writer is registered",
          survived and crashed_tmp not in kept and crashed_tmp in after
          and not os.path.exists(crashed_tmp),
          f"kept while writing={survived}, then removed={crashed_tmp in after}")

    # (d) a save that genuinely cannot be written has to reach the user: the
    # samples are playable, so `ready` stays true, and the only way anyone finds
    # out is `bank.error` (the tray's status line reads it).
    doomed = tiny_bank("grand", 8000)
    doomed.ready = True
    blocker = os.path.join(_HOME, "not-a-directory")
    with open(blocker, "w") as fh:
        fh.write("in the way")
    real_dir = samplebank.CACHE_DIR
    samplebank.CACHE_DIR = os.path.join(blocker, "cache")
    try:
        saved_ok = doomed._try_save_cache()
    finally:
        samplebank.CACHE_DIR = real_dir
    check("a save that fails for real is reported, not swallowed",
          saved_ok is False and doomed.error
          and doomed.error.startswith("sample cache not written")
          and doomed.ready and doomed.get_pair(60) is not None,
          f"error={doomed.error!r}")
    check("...and the registry is not left holding the stem it failed on",
          doomed._stem not in samplebank._WRITING
          and samplebank.prune_cache() is not None,
          f"_WRITING={sorted(samplebank._WRITING)}")

    # ---- 6. the mmap is prefaulted off the audio thread --------------------
    # `note_on` runs on the rtmidi thread, where a major fault costs that one note
    # a fraction of a millisecond; the same fault inside the callback cost 7.9 ms
    # of a 5.8 ms block (48 major faults) on the first note after an idle period.
    advised: list[tuple[int, int, int]] = []

    class map_spy:
        """Forwards to the real mmap, recording what was advised."""

        def __init__(self, mapping) -> None:
            self._mapping = mapping

        def madvise(self, option, start, length):
            advised.append((option, start, length))
            return self._mapping.madvise(option, start, length)

        def __getattr__(self, name):
            return getattr(self._mapping, name)

    faulted: list[int] = []
    real_prefault = SampleBank.prefault

    def prefault_spy(self, note):
        faulted.append(note)
        return real_prefault(self, note)

    real_map = bank._map
    bank._map = map_spy(real_map)
    SampleBank.prefault = prefault_spy
    try:
        pf = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off")
        pf.note_on(60, 100)
        after_note = list(faulted), list(advised)
        pf.note_on(19, 100)          # rejected: nothing to fault in
        pf.note_off(60)
        for _ in range(8):
            pf.render(256)
        during_render = len(faulted)
    finally:
        SampleBank.prefault = real_prefault
        bank._map = real_map
    check("note_on prefaults the key it is about to play, on the MIDI thread",
          after_note[0] == [60] and during_render == len(after_note[0]),
          f"prefaulted {faulted}, {during_render - len(after_note[0])} of them "
          f"from render()")
    wanted = [bank._map_ranges[(60, layer)] for layer in tone.LAYERS]
    covered = all(
        any(opt == mmap.MADV_WILLNEED
            and start <= span[0] and start + length >= span[0] + span[1]
            and start % mmap.PAGESIZE == 0
            for opt, start, length in after_note[1])
        for span in wanted)
    check("...advising exactly the byte ranges of that key's two buffers",
          len(after_note[1]) == 2 and covered
          and all(length <= span[1] + mmap.PAGESIZE
                  for (_o, _s, length), span in zip(after_note[1], wanted)),
          f"{[(s, l) for _o, s, l in after_note[1]]} for spans {wanted}")
    # And the prefault must not be able to dirty what it touches: a written page
    # is a private copy, which is the mmap's whole purpose defeated.
    slices = [bank.get(n, l) for n in (21, 60, 108) for l in tone.LAYERS]
    check("nothing in the mapping is writeable: blob, mapping root or slices",
          not bank._blob.flags.writeable
          and not any(s.flags.writeable for s in slices)
          and _raises(bank.prefault, 60) is None,
          f"{len(slices)} slices checked")
    check("...so a scribble on a sample buffer raises instead of forking a page",
          isinstance(_raises(slices[0].__setitem__, 0, 1), ValueError))


def _raises(func, *args):
    """The exception `func(*args)` raised, or None if it did not raise."""
    try:
        func(*args)
    except BaseException as exc:
        return exc
    return None


def _safe(func, *args):
    """`func(*args)`, or None if it blew up - so one broken key cannot take the
    rest of the section with it."""
    try:
        return func(*args)
    except BaseException:
        return None


# ------------------------------------------------- tuning + strike variation
def test_tuning(bank: SampleBank) -> None:
    """Stretch tuning (`tone.stretch_cents`) and per-strike variation.

    The two live at opposite ends of the pipeline -- one decides what gets
    rendered, the other what playback does with it -- but they answer the same
    complaint, so they are checked together.
    """
    print("\n[12] tuning and strike variation")

    # ---- the curve itself.
    a4 = tone.tuned_freq(69) * math.sqrt(1.0 + tone.inharmonicity(69))
    check("A4's partial 1 lands on 440.000 Hz, which is what a fork gives",
          abs(a4 - 440.0) < 1e-3, f"{a4:.4f} Hz")

    meter = [tone.railsback_cents(n) for n in range(21, 109)]
    check("the tuning curve rises monotonically across the keyboard",
          all(b >= a - 1e-9 for a, b in zip(meter, meter[1:])),
          f"A0 {meter[0]:+.1f} c, C8 {meter[-1]:+.1f} c")
    check("...and lands in the range a tuner would leave a grand in",
          -5.0 < meter[0] < 1.0 and 20.0 < meter[-1] < 50.0,
          f"A0 {meter[0]:+.2f} c, C8 {meter[-1]:+.2f} c")
    # Slope is not ripple: a real top octave does climb 2-3 cents per semitone,
    # so what has to stay small is the *change* in the step, i.e. how far one
    # semitone sits off the line its neighbours draw. Solving the octave
    # constraint exactly instead of smoothing it needs 2.5 c of this, which is
    # the melodic unevenness `tone._STRETCH_STEP` exists to avoid.
    step = [b - a for a, b in zip(meter, meter[1:])]
    ripple = max(abs(b - a) for a, b in zip(step, step[1:]))
    check("no semitone is stretched out of line with its neighbours",
          ripple < 0.6, f"worst bend {ripple:.3f} c, steepest run "
                        f"{max(step):.2f} c/semitone")

    # The whole point: partial 2 of a key should land on partial 1 of the key an
    # octave above it, or the two beat against each other.
    def octave_beat(n, freq):
        b1, b2 = tone.inharmonicity(n), tone.inharmonicity(n + 12)
        return abs(2.0 * freq(n) * math.sqrt(1.0 + 4.0 * b1)
                   - freq(n + 12) * math.sqrt(1.0 + b2))

    was = [octave_beat(n, tone.midi_to_freq) for n in range(24, 97, 12)]
    now = [octave_beat(n, tone.tuned_freq) for n in range(24, 97, 12)]
    check("every octave beats no faster than it did under equal temperament",
          all(b <= a + 0.05 for a, b in zip(was, now)),
          " ".join(f"{n}:{a:.2f}->{b:.2f}" for n, a, b
                   in zip(range(24, 97, 12), was, now)))
    check("...and the top octave, which was audibly out, is four times closer",
          now[-1] < was[-1] / 4.0, f"{was[-1]:.1f} Hz -> {now[-1]:.1f} Hz")
    check("octaves below the break are beatless to well under a hertz",
          max(now[:4]) < 0.1, f"worst {max(now[:4]):.4f} Hz")

    check("an electric voicing is left alone: a tine has no Railsback curve",
          all(tone.stretch_cents(n, v) == 0.0
              for v in ("rhodes", "wurlitzer") for n in (21, 60, 108)))
    check("a shorter, more inharmonic instrument is tuned further out",
          tone.railsback_cents(108, "upright") > tone.railsback_cents(108, "grand"),
          f"upright {tone.railsback_cents(108, 'upright'):.1f} c vs "
          f"grand {tone.railsback_cents(108, 'grand'):.1f} c")

    # ---- strike variation: the mixer.
    def eng(var):
        return AudioEngine(bank, volume=1.0, reverb_preset="off",
                           strike_variation=var)

    def strike(engine, note=60, vel=80, blocks=12):
        engine.all_notes_off(immediate=True)
        for _ in range(40):
            engine.render(256)
        engine.note_on(note, vel)
        return np.concatenate([engine.render(256) for _ in range(blocks)])

    off = eng(0.0)
    a, b = strike(off), strike(off)
    check("with variation off two strikes are bit-identical, as they always were",
          np.array_equal(a, b))

    on = eng(1.0)
    c, d = strike(on), strike(on)
    check("with variation on they are not", not np.array_equal(c, d))
    diff = 20.0 * math.log10(float(np.sqrt(((c - d) ** 2).mean()))
                             / max(float(np.abs(c).max()), 1e-12))
    check("...but they differ by an amount you would call one pianist, not two",
          -40.0 < diff < -12.0, f"{diff:.1f} dB between strikes")
    check("the variation is bounded: no strike is much louder than another",
          abs(float(np.abs(c).max()) / float(np.abs(d).max()) - 1.0) < 0.12,
          f"peak ratio {float(np.abs(c).max()) / float(np.abs(d).max()):.4f}")

    # The interpolating read is the part that can go wrong quietly.
    check("a detuned voice stays finite and does not clip",
          np.isfinite(c).all() and float(np.abs(c).max()) < 1.0,
          f"peak {float(np.abs(c).max()):.4f}")

    long_run = eng(1.0)
    long_run.all_notes_off(immediate=True)
    for note in range(21, 109):
        long_run.note_on(note, 100)
    long_run.control_change(64, 127)
    tail = np.concatenate([long_run.render(256) for _ in range(2400)])
    check("every detuned voice runs off the end of its buffer and is reaped",
          long_run.active_voices == 0 and np.isfinite(tail).all(),
          f"{long_run.active_voices} voices left after 14 s")

    # Determinism: an exported take has to be the take that was played.
    check("the variation is arbitrary, not random -- two runs agree",
          np.array_equal(strike(eng(1.0)), strike(eng(1.0))))

    # ---- sympathetic resonance.
    def res_engine(depth=1.0):
        return AudioEngine(bank, volume=1.0, reverb_preset="off",
                           strike_variation=0.0, resonance=depth)

    def chord(engine, pedal=True, hold=120, after=400):
        if pedal:
            engine.control_change(64, 127)
        for note in (48, 55, 64):
            engine.note_on(note, 100)
        out = [engine.render(256) for _ in range(hold)]
        for note in (48, 55, 64):
            engine.note_off(note)
        out += [engine.render(256) for _ in range(after)]
        return np.concatenate(out)

    loud, silent_bank = chord(res_engine(1.0)), chord(res_engine(0.0))
    m = min(len(loud), len(silent_bank))
    halo = loud[:m] - silent_bank[:m]
    rel = 20.0 * math.log10(float(np.sqrt((halo ** 2).mean()))
                            / float(np.sqrt((silent_bank[:m] ** 2).mean())))
    check("the undamped strings answer, and at an instrument's level",
          -20.0 < rel < -9.0, f"{rel:.1f} dB under the notes driving them")
    # Side/mid, not correlation: the two halves of the bank are spread across
    # the soundboard rather than decorrelated from each other, so they keep a
    # large common component and the correlation stays high (~0.97) while the
    # image is genuinely wide. Correlation is the wrong instrument for this.
    h_mid = (halo[:, 0] + halo[:, 1]) * 0.5
    h_side = (halo[:, 0] - halo[:, 1]) * 0.5
    h_width = 20.0 * math.log10(float(np.sqrt((h_side ** 2).mean()))
                                / max(float(np.sqrt((h_mid ** 2).mean())), 1e-12))
    check("...arriving spread across the soundboard, not from one point",
          h_width > -25.0,
          f"halo side/mid {h_width:.1f} dB, L/R correlation "
          f"{float(np.corrcoef(halo[:, 0], halo[:, 1])[0, 1]):.3f}")
    check("depth 0 is bit-identical to having no bank at all",
          np.array_equal(chord(res_engine(0.0)), silent_bank))

    # With the pedal up nothing is undamped, so there is nothing to answer.
    up = chord(res_engine(1.0), pedal=False)
    flat = chord(res_engine(0.0), pedal=False)
    m = min(len(up), len(flat))
    check("with the damper pedal up the bank is silent, not merely quiet",
          np.array_equal(up[:m], flat[:m]))

    # The bank must not outlive the pedal.
    lift = res_engine(1.0)
    lift.control_change(64, 127)
    for note in (36, 48, 55):
        lift.note_on(note, 110)
    for _ in range(200):
        lift.render(256)
    for note in (36, 48, 55):
        lift.note_off(note)
    for _ in range(60):
        lift.render(256)
    lift.control_change(64, 0)
    settled = np.concatenate([lift.render(256) for _ in range(900)])
    check("lifting the pedal damps the sympathetic strings too",
          float(np.abs(settled[-44100:]).max()) < 1e-4,
          f"residual {float(np.abs(settled[-44100:]).max()):.2e}")

    # An immediate cut, measured on the samples: this is the case `bass_engine`
    # cannot check, and the reason it turns the bank off.
    for label, cut in (("CC120", lambda e: e.control_change(120, 0)),
                       ("panic", lambda e: e.all_notes_off(immediate=True))):
        eng_cut = res_engine(1.0)
        eng_cut.control_change(64, 127)
        for note in (36, 48, 55, 64):
            eng_cut.note_on(note, 110)
        for _ in range(200):
            before = eng_cut.render(1024)
        cut(eng_cut)
        during = eng_cut.render(1024)
        after_cut = eng_cut.render(1024)
        seam = float(np.abs(during[0] - before[-1]).max())
        worst = float(np.abs(np.diff(during, axis=0)).max())
        check(f"{label} with the bank running leaves no step and then silence",
              seam < 0.01 and worst < 0.01
              and float(np.abs(after_cut).max()) == 0.0,
              f"seam {seam:.5f}, worst step {worst:.5f}, "
              f"next block peak {float(np.abs(after_cut).max()):.1e}")

    # Worst case: everything down, everything loud, biggest room.
    hard = AudioEngine(bank, volume=1.0, reverb_preset="hall", resonance=1.0)
    hard.control_change(64, 127)
    for note in range(21, 109):
        hard.note_on(note, 127)
    blast = np.concatenate([hard.render(256) for _ in range(700)])
    check("eighty-eight keys fff under the pedal stays finite and in range",
          np.isfinite(blast).all() and float(np.abs(blast).max()) <= 1.0,
          f"peak {float(np.abs(blast).max()):.3f}")

    quiet = eng(1.0)
    quiet.all_notes_off(immediate=True)
    for _ in range(40):
        quiet.render(256)
    quiet.note_on(60, 80)
    first = quiet.render(64)
    check("the first block of a detuned voice reads no sample before the buffer",
          np.isfinite(first).all() and float(np.abs(first).max()) > 0.0,
          f"peak {float(np.abs(first).max()):.4f}")


# --------------------------------------------------------------- recordings
def test_sampled() -> None:
    """The sampled grand: the pack, the mapping, and what the mixer does with it.

    Everything here is skipped rather than failed when the pack or `soundfile` is
    absent, because both are legitimately missing from a source checkout -- and
    `make_bank` falling back to the model in that case is itself checked.
    """
    print("\n[13] sampled instruments")
    from justpiano import config as jp_config, sampled

    # ---- every pack's manifest has to cover the keyboard on its own terms.
    installed = [v for v in sampled.VOICINGS if sampled.pack_present(v)]
    for voicing in installed:
        meta = sampled.load_manifest(voicing)
        layers = len(meta["layer_velocities"])
        gaps, worst = [], 0.0
        for layer in range(layers):
            covered = set()
            for region in meta["regions"]:
                if int(region["layer"]) != layer:
                    continue
                lo, hi, kc = (int(region["lo"]), int(region["hi"]),
                              int(region["kc"]))
                covered |= set(range(lo, hi + 1))
                worst = max(worst, abs(lo - kc), abs(hi - kc))
            gaps += [n for n in range(tone.NOTE_MIN, tone.NOTE_MAX + 1)
                     if n not in covered]
        check(f"{voicing}: every key is covered at every velocity layer",
              not gaps, f"{len(gaps)} keys unreachable")
        check(f"{voicing}: no key is stretched further than the pack intends",
              worst <= 3, f"worst shift {worst:.0f} semitones")
        check(f"{voicing}: velocity layers are in order and inside MIDI range",
              meta["layer_velocities"] == sorted(meta["layer_velocities"])
              and 0 < meta["layer_velocities"][0]
              and meta["layer_velocities"][-1] <= 127.0,
              str(meta["layer_velocities"]))
        check(f"{voicing}: the pack says who made it, under what, and where",
              bool(meta.get("credit")) and bool(meta.get("licence"))
              and str(meta.get("url", "")).startswith("https://"),
              f"{meta.get('licence')}")

    check("both instruments are credited, whatever their licence asks for",
          len(sampled.credits()) >= 1
          and any("Alexander Holm" in c for c in sampled.credits()),
          f"{len(sampled.credits())} credits")

    if not installed:
        check("no packs installed: make_bank falls back to the model",
              not isinstance(sampled.make_bank("grand"), sampled.SampledBank))
        print("     (packs absent - the rest of [13] needs assets/samples)")
        return
    try:
        import soundfile  # noqa: F401
    except Exception:
        print("     (soundfile absent - the rest of [13] needs it)")
        return

    check("a voicing with no recordings still comes back as a modelled bank",
          not isinstance(sampled.make_bank("wurlitzer"), sampled.SampledBank))

    bank = sampled.make_bank("grand")
    check("grand is served from recordings when the pack is there",
          isinstance(bank, sampled.SampledBank) and bank.stereo)
    t0 = time.time()
    bank.build_blocking()
    check("the pack decodes and the bank comes up mapped, not on the heap",
          bank.ready and bank.error is None and bank.mapped,
          f"{time.time() - t0:.2f}s, error={bank.error}")
    check("a second bank is served from the cache without decoding again",
          sampled.SampledBank("grand")._load_cache() is True)

    # ---- dynamics: louder velocity must never come back quieter.
    # `layer_mix` is the one that answers in layer *indices*; `note_mix` hands
    # back the buffers themselves, which is what the mixer wants and what makes
    # it the wrong call to reason about ordering with.
    last, monotone = -1.0, True
    for vel in range(1, 128):
        v = 127.0 * (vel / 127.0) ** 1.30
        lo, hi, blend, gain = bank.layer_mix(v)
        # where this blow lands on the ladder of recordings, softest to loudest
        level = (lo + blend) * gain
        if level < last - 1e-6:
            monotone = False
        last = level
    check("harder always means louder across the whole velocity range", monotone)
    top = len(bank.layer_velocities) - 1
    check("the softest and hardest blows reach the end recordings",
          bank.layer_mix(1.0)[0] == 0 and bank.layer_mix(127.0)[1] == top,
          f"{bank.layer_mix(1.0)[:2]} .. {bank.layer_mix(127.0)[:2]} of 0..{top}")

    a, b, w_lo, w_hi, step, amp = bank.note_mix(61, 90.0, 1.30)
    check("a mixed note hands the mixer two stereo buffers and a rate",
          a.ndim == 2 and a.shape[1] == 2 and a.dtype == np.int16
          and abs(w_lo + w_hi - 1.0) < 1e-6 and abs(step - 2 ** (1 / 12)) < 1e-6,
          f"{a.shape} {a.dtype}, weights {w_lo:.2f}/{w_hi:.2f}, step {step:.5f}")
    check("a key on a recorded pitch is played at its own speed",
          bank.note_mix(60, 90.0, 1.30)[4] == 1.0)
    bank.prefault(61)
    check("the release and pedal recordings are there", 
          bank.release(60) is not None and bank.pedal_noise(True) is not None
          and bank.pedal_noise(False) is not None)

    # ---- what the engine does with it.
    def chord(bk, vel=90, notes=(48, 55, 64, 72), preset="off", blocks=300):
        eng = AudioEngine(bk, blocksize=256, volume=0.9, reverb_preset=preset,
                          strike_variation=0.0, resonance=0.0)
        for note in notes:
            eng.note_on(note, vel)
        return np.concatenate([eng.render(256) for _ in range(blocks)])

    played = chord(bank)
    left, right = played[:, 0], played[:, 1]
    side = (left - right) * 0.5
    mid = (left + right) * 0.5
    width = 20.0 * math.log10(float(np.sqrt((side ** 2).mean()))
                              / max(float(np.sqrt((mid ** 2).mean())), 1e-12))
    check("the recorded stereo image survives the mixer",
          width > -8.0 and np.isfinite(played).all(),
          f"side/mid {width:.1f} dB (the modelled bank managed -24.7)")

    # ---- level parity, so the Instrument menu is not a volume control.
    modelled = SampleBank("grand")
    modelled.build_blocking()
    worst_gap = 0.0
    for vel in (40, 90, 115):
        m = float(np.sqrt((chord(modelled, vel) ** 2).mean()))
        r = float(np.sqrt((chord(bank, vel) ** 2).mean()))
        worst_gap = max(worst_gap, abs(20.0 * math.log10(r / max(m, 1e-12))))
    check("switching between a recorded and a modelled instrument is not a jump",
          worst_gap < 3.5, f"worst {worst_gap:.1f} dB apart")

    loud = chord(bank, 127, notes=tuple(range(21, 109)), preset="hall")
    check("eighty-eight recorded keys fff stay in range",
          np.isfinite(loud).all() and float(np.abs(loud).max()) <= 1.0,
          f"peak {float(np.abs(loud).max()):.3f}")

    # ---- the mechanical noises, which no key owns.
    quiet = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off",
                        strike_variation=0.0, resonance=0.0)
    quiet.note_on(60, 100)
    for _ in range(20):
        quiet.render(256)
    quiet.note_off(60)
    for _ in range(400):
        quiet.render(256)
    check("letting a key go sounds a damper, and it retires on its own",
          quiet.active_voices == 0
          and not [v for v in quiet._oneshots if not v.dead],
          f"{len(quiet._oneshots)} one-shots held")

    pedalled = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off",
                           strike_variation=0.0, resonance=0.0)
    pedalled.control_change(64, 127)
    with_pedal = np.concatenate([pedalled.render(256) for _ in range(60)])
    check("the damper pedal makes a sound of its own, with nothing played",
          float(np.abs(with_pedal).max()) > 1e-4,
          f"peak {float(np.abs(with_pedal).max()):.4f}")

    # A key released under the pedal is not damped, so nothing should thud.
    held = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off",
                       strike_variation=0.0, resonance=0.0)
    held.control_change(64, 127)
    for _ in range(80):
        held.render(256)
    before = len([v for v in held._oneshots if not v.dead])
    held.note_on(60, 100)
    for _ in range(20):
        held.render(256)
    held.note_off(60)
    check("a key let go under the pedal makes no damper noise, as on the piano",
          len([v for v in held._oneshots if not v.dead]) == before)

    # A panic has to reach voices no key owns.
    panic = AudioEngine(bank, blocksize=256, volume=1.0, reverb_preset="off",
                        strike_variation=0.0, resonance=0.0)
    panic.control_change(64, 127)
    for note in (48, 55):
        panic.note_on(note, 110)
    for _ in range(30):
        panic.render(256)
    panic.all_notes_off(immediate=True)
    after = np.concatenate([panic.render(1024) for _ in range(200)])
    check("a panic silences the pedal recording too, not just the notes",
          float(np.abs(after[-44100:]).max()) == 0.0,
          f"residual {float(np.abs(after[-44100:]).max()):.2e}")

    # ---- the instruments have to be told apart by ear, so measure it.
    def centroid(block):
        mono = block.mean(axis=1)[:1 << 16]
        mag = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        freq = np.fft.rfftfreq(len(mono), 1.0 / 44100)
        return float((mag * freq).sum() / max(mag.sum(), 1e-9))

    voices = {}
    for name in ("grand", "felt", "upright"):
        if not sampled.pack_present(name):
            continue
        bk = sampled.make_bank(name)
        bk.build_blocking()
        block = chord(bk)
        voices[name] = (bk, block, centroid(block),
                        float(np.sqrt((block ** 2).mean())))

    if "felt" in voices:
        g_c, g_r = voices["grand"][2], voices["grand"][3]
        f_c, f_r = voices["felt"][2], voices["felt"][3]
        check("felt is unmistakably darker than the grand it comes from",
              f_c < g_c * 0.72,
              f"centroid {f_c:.0f} Hz against {g_c:.0f} Hz "
              f"({20 * math.log10(f_r / g_r):+.1f} dB level)")
        check("...and softer, because a moderator is felt and not an equaliser",
              -10.0 < 20.0 * math.log10(f_r / g_r) < -3.0,
              f"{20 * math.log10(f_r / g_r):+.1f} dB")
    if "upright" in voices:
        u_bank, _, u_c, u_r = voices["upright"]
        check("the upright is its own recording, not the grand filtered",
              u_bank.meta.get("instrument", "").lower().find("upright") >= 0
              and u_bank.meta is not voices["grand"][0].meta
              and len(u_bank.layer_velocities) != len(
                  voices["grand"][0].layer_velocities),
              f"{u_bank.meta.get('instrument')}, "
              f"{len(u_bank.layer_velocities)} layers")
        # Across the velocity range, not at one chord. Two recorded layers do
        # not carry a piano's dynamics on their own, and a single mid-velocity
        # measurement hid the upright being 10 dB too loud at pianissimo -- see
        # `SampledBank._dynamic_gain`.
        offsets = []
        for vel in (30, 50, 70, 85, 100, 115, 127):
            g = float(np.sqrt((chord(voices["grand"][0], vel) ** 2).mean()))
            u = float(np.sqrt((chord(u_bank, vel) ** 2).mean()))
            offsets.append(20.0 * math.log10(u / max(g, 1e-12)))
        mean = sum(offsets) / len(offsets)
        check("...and tracks the grand's level across the whole velocity range",
              abs(mean) < 2.0 and max(abs(o) for o in offsets) < 5.0,
              "mean %+.1f dB, worst %+.1f dB (%s)"
              % (mean, max(offsets, key=abs),
                 " ".join("%+.0f" % o for o in offsets)))
        check("a two-layer pack is given back the dynamics it cannot record",
              u_bank.meta.get("dynamic_db", 0) > 0
              and u_bank._dynamic_gain(127.0) == 1.0
              and u_bank._dynamic_gain(20.0) < 0.5,
              f"{u_bank.meta.get('dynamic_db')} dB of tilt")

        # Layers from independently laid-out regions are not the same length,
        # and reading the shorter at the longer's index is an IndexError inside
        # the audio callback. This is the case that found it.
        pairs = [(u_bank.note_mix(n, 90.0, 1.30)) for n in range(21, 109)]
        ragged = sum(1 for p in pairs if p and len(p[0]) != len(p[1]))
        eng = AudioEngine(u_bank, blocksize=256, volume=1.0, reverb_preset="off",
                          strike_variation=1.0, resonance=0.0, key_noise=0.0)
        for note in range(21, 109):
            eng.note_on(note, 90)
        swept = np.concatenate([eng.render(256) for _ in range(900)])
        check("mismatched layer lengths do not run the reader off the end",
              np.isfinite(swept).all() and eng.active_voices == 0,
              f"{ragged} of 88 keys have layers of differing length")
        loud = chord(u_bank, 127, notes=tuple(range(21, 109)))
        check("the upright survives eighty-eight keys fff without clipping",
              float(np.abs(loud).max()) <= 1.0
              and int((np.abs(loud) >= 0.999).sum()) == 0,
              f"peak {float(np.abs(loud).max()):.3f}")

    # ---- the action level is the user's, and the instrument's.
    quiet_bank = voices["grand"][0]
    def action_rms(level):
        eng = AudioEngine(quiet_bank, blocksize=256, volume=1.0,
                          reverb_preset="off", strike_variation=0.0,
                          resonance=0.0, key_noise=level)
        eng.control_change(64, 127)
        return float(np.sqrt((np.concatenate(
            [eng.render(256) for _ in range(80)]) ** 2).mean()))
    check("key noise at 0 is silence, not just quiet", action_rms(0.0) == 0.0)
    louder = action_rms(0.5)
    softer = action_rms(0.12)
    check("...and the setting actually moves it",
          louder > softer > 0.0,
          f"0.12 -> {softer:.5f}, 0.50 -> {louder:.5f}")
    check("the default is well under where the microphones put it",
          0.0 < jp_config.DEFAULTS["key_noise"] <= 0.3,
          f"{jp_config.DEFAULTS['key_noise']}")


def main() -> int:
    print("Just Piano self-test")
    bank = test_bank()
    test_engine(bank)
    test_locking(bank)
    test_reverb(bank)
    test_reverb_gate(bank)
    test_recorder(bank)
    test_output_stream(bank)
    test_native_helpers()
    test_voicings(bank)
    test_footprint(bank)
    test_keyboard()
    test_mute(bank)
    test_tuning(bank)
    test_sampled()
    test_hotkey()
    test_icons()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
