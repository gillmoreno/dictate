#!/usr/local/bin/python3.11
"""Record a clip, transcribe it once, and paste the text.

The microphone stays closed until you double-tap Option. Double-tap Option
again, or press Escape, to stop. The clip is saved as a wav file, Phonon-2
transcribes that file on the Neural Engine, and the transcript is pasted
where the cursor is. The same text stays on the clipboard. Ctrl-C quits.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

ESCAPE_KEYCODE = 0x35
V_KEYCODE = 0x09
COMMAND_KEYCODE = 0x37
COMMAND_FLAG = 0x00100000
HID_SOURCE = 1
HID_POST = 0
KEY_DOWN = 10
KEY_UP = 11
FLAGS_CHANGED = 12
CLIP_PATH = Path.home() / ".cache" / "dictate" / "last.wav"
ROOT = Path(__file__).resolve().parent
ENGINE_DIR = ROOT / "engine"
ENGINE_BIN = ENGINE_DIR / ".build" / "release" / "dictate-engine"
MODEL_DIR = Path.home() / ".cache" / "dictate" / "Phonon-2-CoreML"
MODEL_REPO = "FermionResearch/Phonon-2-CoreML"
SAMPLE_RATE = 16_000
MIN_CLIP_S = 0.25
SILENCE_RMS = 0.004
ALT_FLAG = 0x00080000
# Letters cancel a half-finished double-tap. These modifiers do not.
MODIFIER_KEYS = frozenset({
    0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
})
TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF)
MAX_HOLD_S = 0.50
DOUBLE_TAP_S = 0.60


def pcm16(samples) -> bytes:
    """Mono float samples in [-1, 1], as little-endian 16-bit PCM."""
    try:
        import numpy as np
        audio = np.clip(np.asarray(samples, dtype="float64"), -1.0, 1.0)
        return (audio * 32767.0).astype("<i2").tobytes()
    except ModuleNotFoundError:
        pass
    import array
    out = array.array("h")
    for sample in samples:
        value = float(sample)
        if value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0
        out.append(int(round(value * 32767.0)))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def write_wav(path: Path, samples, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm16(samples))


def clip_level(samples) -> float:
    try:
        import numpy as np
        audio = np.asarray(samples, dtype="float64")
        if int(audio.size) == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio))))
    except ModuleNotFoundError:
        pass
    total = 0.0
    count = 0
    for sample in samples:
        value = float(sample)
        total += value * value
        count += 1
    if count == 0:
        return 0.0
    return (total / count) ** 0.5


def worth_transcribing(samples, rate: int) -> bool:
    count = int(getattr(samples, "size", len(samples)))
    if count < int(MIN_CLIP_S * rate):
        return False
    return clip_level(samples) >= SILENCE_RMS


def copy_text(text: str) -> None:
    subprocess.run(
        ["/usr/bin/pbcopy"],
        input=text.encode("utf-8"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class Paster:
    """Press Command-V in the focused app. Needs Accessibility permission."""

    def __init__(self):
        self._cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._app = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cg, cf = self._cg, self._cf
        cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
        cg.CGEventSourceCreate.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
        cg.CGEventGetFlags.restype = ctypes.c_uint64
        cg.CGEventGetIntegerValueField.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32]
        cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._app.AXIsProcessTrusted.restype = ctypes.c_bool

    def trusted(self) -> bool:
        return bool(self._app.AXIsProcessTrusted())

    def command_v_event(self, source, down: bool):
        event = self._cg.CGEventCreateKeyboardEvent(source, V_KEYCODE, down)
        if not event:
            raise SystemExit("could not create a paste keystroke")
        self._cg.CGEventSetFlags(event, COMMAND_FLAG)
        return event

    def paste(self) -> None:
        if not self.trusted():
            raise PermissionError("accessibility")
        source = self._cg.CGEventSourceCreate(HID_SOURCE)
        if not source:
            raise SystemExit("could not create a keyboard event source")
        cg, cf = self._cg, self._cf
        try:
            chord = (
                (COMMAND_KEYCODE, True, COMMAND_FLAG),
                (V_KEYCODE, True, COMMAND_FLAG),
                (V_KEYCODE, False, COMMAND_FLAG),
                (COMMAND_KEYCODE, False, 0),
            )
            for keycode, down, flags in chord:
                event = cg.CGEventCreateKeyboardEvent(source, keycode, down)
                if not event:
                    raise SystemExit("could not create a paste keystroke")
                cg.CGEventSetFlags(event, flags)
                cg.CGEventPost(HID_POST, event)
                cf.CFRelease(event)
                if keycode == V_KEYCODE and down:
                    time.sleep(0.02)
        finally:
            cf.CFRelease(source)


def _announce(text: str, sound: str | None = None) -> None:
    print(text, file=sys.stderr, flush=True)
    if not sound:
        return
    path = f"/System/Library/Sounds/{sound}"
    if os.path.isfile(path):
        subprocess.Popen(
            ["/usr/bin/afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class OptionDoubleTap:
    """Two quick Option presses, with no letter in between.

    Tracks the Option flag itself, so a left tap then a right tap still
    counts, and a missing key code does not drop the press. Option held
    while another key is typed is a normal shortcut, not a tap.
    """

    def __init__(self, on_toggle, *, max_hold=MAX_HOLD_S, window=DOUBLE_TAP_S):
        self.on_toggle = on_toggle
        self.max_hold = max_hold
        self.window = window
        self.alt_down = False
        self.down_at = 0.0
        self.chord = False
        self.armed_at: float | None = None

    def event(self, type_, keycode, alt_down, now) -> None:
        if type_ in (KEY_DOWN, KEY_UP) and keycode not in MODIFIER_KEYS:
            self.chord = True
            self.armed_at = None
        if type_ == FLAGS_CHANGED or keycode in MODIFIER_KEYS:
            self._set_alt(bool(alt_down), now)

    def _set_alt(self, down: bool, now: float) -> None:
        if down and not self.alt_down:
            self.alt_down = True
            self.down_at = now
            self.chord = False
            return
        if down or not self.alt_down:
            return
        self.alt_down = False
        held = now - self.down_at
        if self.chord or held < 0.02 or held > self.max_hold:
            self.chord = False
            self.armed_at = None
            return
        if self.armed_at is not None and now - self.armed_at <= self.window:
            self.armed_at = None
            self.on_toggle()
            return
        self.armed_at = now


class HotkeyTap:
    """Listen-only tap on the main thread. Keys still reach the focused app."""

    def __init__(self, on_toggle, on_escape):
        self.gesture = OptionDoubleTap(on_toggle)
        self.on_escape = on_escape
        self._last_escape = 0.0
        self._cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self._cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._cb_type = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_void_p)
        self._callback = self._cb_type(self._on_event)
        cg, cf = self._cg, self._cf
        cg.CGEventTapCreate.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
            self._cb_type, ctypes.c_void_p]
        cg.CGEventTapCreate.restype = ctypes.c_void_p
        cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
        cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
        cg.CGEventGetFlags.restype = ctypes.c_uint64
        cf.CFMachPortCreateRunLoopSource.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRunInMode.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        listen = getattr(cg, "CGRequestListenEventAccess", None)
        if listen is not None:
            listen.restype = ctypes.c_bool
            listen()
        # These are specific objects. A new string with the same characters
        # is a different mode, and the tap then never sees a key.
        self._common_modes = ctypes.c_void_p.in_dll(cf, "kCFRunLoopCommonModes")
        self._default_mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        self.tap = None

    def start(self) -> bool:
        """Install the tap on the calling thread. That thread must pump()."""
        if not self._common_modes.value or not self._default_mode.value:
            return False
        mask = (1 << KEY_DOWN) | (1 << KEY_UP) | (1 << FLAGS_CHANGED)
        self.tap = self._cg.CGEventTapCreate(
            1, 0, 1, ctypes.c_uint64(mask), self._callback, None)
        if not self.tap:
            return False
        # Common modes so the tap stays scheduled if this thread leaves the
        # default mode. RunInMode cannot take the common-modes set itself.
        source = self._cf.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        self._cf.CFRunLoopAddSource(
            self._cf.CFRunLoopGetCurrent(), source, self._common_modes)
        self._cg.CGEventTapEnable(self.tap, True)
        return True

    def pump(self, seconds: float = 0.25) -> None:
        self._cf.CFRunLoopRunInMode(self._default_mode, seconds, False)

    def _on_event(self, _proxy, type_, event, _user):
        try:
            if type_ in TAP_DISABLED and self.tap:
                self._cg.CGEventTapEnable(self.tap, True)
                return event
            if type_ not in (KEY_DOWN, KEY_UP, FLAGS_CHANGED):
                return event
            keycode = int(self._cg.CGEventGetIntegerValueField(event, 9))
            alt_down = bool(self._cg.CGEventGetFlags(event) & ALT_FLAG)
            now = time.monotonic()
            if type_ == KEY_DOWN and keycode == ESCAPE_KEYCODE:
                if now - self._last_escape >= 0.3:
                    self._last_escape = now
                    self.on_escape()
            self.gesture.event(type_, keycode, alt_down, now)
        except Exception:
            pass
        return event


class Dictation:
    """One clip at a time. The microphone closes as soon as the take stops."""

    def __init__(self, speech, paster: Paster, clip_path: Path = CLIP_PATH):
        self.speech = speech
        self.paster = paster
        self.clip_path = clip_path
        self.stop_mic = threading.Event()
        self.thread: threading.Thread | None = None
        self.on = False
        self._lock = threading.Lock()

    def toggle(self) -> None:
        with self._lock:
            if self.on:
                self._request_stop()
                return
            if self.thread is not None and self.thread.is_alive():
                _announce("still transcribing the last clip")
                return
            self.on = True
            self.stop_mic.clear()
            self.thread = threading.Thread(
                target=self._record, name="dictate-mic", daemon=True)
            self.thread.start()
        _announce("recording", "Tink.aiff")

    def stop(self) -> None:
        with self._lock:
            self._request_stop()

    def _request_stop(self) -> None:
        if not self.on:
            return
        self.on = False
        self.stop_mic.set()
        _announce("recording stopped")

    def _record(self) -> None:
        me = threading.current_thread()
        audio = None
        try:
            audio = _capture(self.stop_mic)
        except Exception as exc:
            print(f"microphone stopped ({exc}).", file=sys.stderr, flush=True)
        finally:
            with self._lock:
                if self.thread is me:
                    self.on = False
        if audio is None:
            return
        if not worth_transcribing(audio, SAMPLE_RATE):
            _announce("nothing caught on the microphone")
            return
        seconds = int(audio.size) / SAMPLE_RATE
        try:
            write_wav(self.clip_path, audio, SAMPLE_RATE)
        except Exception as exc:
            print(f"could not save the clip ({exc}).", file=sys.stderr, flush=True)
            return
        _announce(f"transcribing {seconds:.1f}s from {self.clip_path}")
        try:
            text, _decode_s, _audio_s = self.speech.transcribe(self.clip_path)
        except Exception as exc:
            print(f"transcription failed ({exc}).", file=sys.stderr, flush=True)
            return
        text = str(text).strip()
        if not text:
            _announce("no words in that clip")
            return
        try:
            copy_text(text)
        except Exception as exc:
            print(text, file=sys.stderr, flush=True)
            print(f"could not copy to the clipboard ({exc}).", file=sys.stderr, flush=True)
            return
        time.sleep(0.05)
        try:
            self.paster.paste()
        except Exception as exc:
            print(text, file=sys.stderr, flush=True)
            print(
                f"could not paste ({exc}). The text is on the clipboard.",
                file=sys.stderr, flush=True)
            return
        _announce(text, "Pop.aiff")


def _capture(stop_mic: threading.Event):
    """Record until stop_mic. None means the microphone could not be opened."""
    import queue

    import numpy as np
    import sounddevice as sd

    blocks: "queue.Queue" = queue.Queue()

    def _on_audio(indata, frames, time_info, status):
        blocks.put(indata[:, 0].copy())

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=int(0.05 * SAMPLE_RATE), callback=_on_audio)
        stream.start()
    except Exception as exc:
        print(
            f"could not open the microphone ({exc}). Grant this terminal "
            "microphone access in System Settings → Privacy & Security → "
            "Microphone, then double-tap Option again.",
            file=sys.stderr, flush=True)
        return None
    chunks = []
    try:
        while not stop_mic.is_set():
            try:
                chunks.append(blocks.get(timeout=0.25))
            except queue.Empty:
                continue
    finally:
        stream.stop()
        stream.close()
    while True:
        try:
            chunks.append(blocks.get_nowait())
        except queue.Empty:
            break
    if not chunks:
        return np.zeros(0, dtype="float32")
    return np.concatenate(chunks)


def _model_ready(path: Path) -> bool:
    weights = path / "Phonon-2.mlpackage" / "Data" / "com.apple.CoreML" / "weights" / "weight.bin"
    return (
        (path / "manifest.json").is_file()
        and (path / "decoder.bin").is_file()
        and weights.is_file()
    )


def ensure_model() -> Path:
    """The Core ML folder, downloaded once into the user cache."""
    if _model_ready(MODEL_DIR):
        return MODEL_DIR
    print(
        "downloading Phonon-2 for the Neural Engine (about 350 MB)…",
        file=sys.stderr, flush=True)
    try:
        from huggingface_hub import snapshot_download
    except ModuleNotFoundError:
        raise SystemExit(
            "huggingface_hub is not installed for this Python.\n"
            "pip install huggingface_hub")
    snapshot_download(
        MODEL_REPO,
        local_dir=str(MODEL_DIR),
        allow_patterns=[
            "manifest.json",
            "decoder.bin",
            "NOTICE",
            "Phonon-2.mlpackage/**",
            "ci/**",
        ],
    )
    if not _model_ready(MODEL_DIR):
        raise SystemExit(
            f"the download finished, but {MODEL_DIR} is not a complete model.")
    return MODEL_DIR


def ensure_engine() -> Path:
    """The release binary. Built once, from the Swift package in this folder."""
    if ENGINE_BIN.is_file():
        return ENGINE_BIN
    if not (ENGINE_DIR / "Package.swift").is_file():
        raise SystemExit(f"the Neural Engine runner is missing at {ENGINE_DIR}")
    if shutil.which("swift") is None:
        raise SystemExit(
            "swift is not installed.\n"
            "Install the Xcode Command Line Tools, then run this again.")
    print("building the Neural Engine runner (once)…", file=sys.stderr, flush=True)
    subprocess.run(
        ["swift", "build", "-c", "release", "--package-path", str(ENGINE_DIR)],
        check=True,
    )
    if not ENGINE_BIN.is_file():
        raise SystemExit("swift build finished without producing dictate-engine")
    return ENGINE_BIN


class CoreMLSpeech:
    """Phonon-2 kept loaded. The encoder stays on the Neural Engine."""

    def __init__(self):
        # The build and the download do not depend on each other.
        errors: list[BaseException] = []

        def _download() -> None:
            try:
                ensure_model()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_download, name="dictate-model")
        thread.start()
        try:
            binary = ensure_engine()
        finally:
            thread.join()
        if errors:
            raise errors[0]
        bundle = MODEL_DIR
        self.proc = subprocess.Popen(
            [str(binary), str(bundle)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("the Neural Engine runner stopped before it was ready.")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"the Neural Engine runner sent {line!r} ({exc}).")
        if not message.get("ready"):
            raise SystemExit(f"the Neural Engine runner did not start: {message}")

    def transcribe(self, path: Path):
        if self.proc.poll() is not None:
            raise RuntimeError("the Neural Engine runner has stopped")
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(f"{path}\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the Neural Engine runner closed its output")
        message = json.loads(line)
        if message.get("error"):
            raise RuntimeError(str(message["error"]))
        return (
            str(message.get("text", "")),
            float(message.get("wall_s", 0.0)),
            float(message.get("audio_s", 0.0)),
        )

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.terminate()


def serve() -> None:
    paster = Paster()
    if not paster.trusted():
        raise SystemExit(
            "this terminal cannot paste into other apps yet.\n"
            "System Settings → Privacy & Security → Accessibility, enable the "
            "app you launched this from (Terminal, iTerm, or Grok), then run it again.\n"
            f"interpreter: {sys.executable}")
    print("loading Phonon-2 on the Neural Engine…", file=sys.stderr, flush=True)
    speech = CoreMLSpeech()
    dictation = Dictation(speech, paster)
    # The tap has to be pumped on this thread. A background run loop was
    # created successfully before and never saw the double-tap.
    hotkeys = HotkeyTap(dictation.toggle, dictation.stop)
    if not hotkeys.start():
        raise SystemExit(
            "could not watch the keyboard.\n"
            "System Settings → Privacy & Security → Input Monitoring, enable "
            "the app you launched this from (Terminal, iTerm, or Grok), "
            "then run it again.")
    print("ready. The microphone is off.", file=sys.stderr, flush=True)
    print("Double-tap Option to record. Double-tap again, or press Escape, to stop.",
          file=sys.stderr, flush=True)
    print("The transcript is pasted at the cursor and left on the clipboard.",
          file=sys.stderr, flush=True)
    print("Ctrl-C quits.", file=sys.stderr, flush=True)
    try:
        while True:
            hotkeys.pump(0.25)
    finally:
        speech.close()


def _check_tap_run_loop(hotkeys: HotkeyTap) -> None:
    """The tap's run loop must actually run.

    Adding the tap to a homemade "kCFRunLoopCommonModes" string succeeds and
    then never delivers a key. The real constant does.
    """
    cf = hotkeys._cf
    fired = {"n": 0}
    cb_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

    def _fire(_timer, _info):
        fired["n"] += 1

    callback = cb_type(_fire)
    cf.CFAbsoluteTimeGetCurrent.restype = ctypes.c_double
    cf.CFRunLoopTimerCreate.argtypes = [
        ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_uint32,
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRunLoopTimerCreate.restype = ctypes.c_void_p
    cf.CFRunLoopAddTimer.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    timer = cf.CFRunLoopTimerCreate(
        None, cf.CFAbsoluteTimeGetCurrent() + 0.02, 0, 0, 0, callback, None)
    if not timer:
        raise SystemExit("could not create a run-loop timer")
    cf.CFRunLoopAddTimer(
        cf.CFRunLoopGetCurrent(), timer, hotkeys._common_modes)
    hotkeys.pump(0.2)
    cf.CFRelease(timer)
    if fired["n"] < 1:
        raise SystemExit(
            "the hotkey run loop never ran. The tap has to be added to the "
            "real kCFRunLoopCommonModes constant.")


def _press_option(gesture: OptionDoubleTap, keycode: int, t_down: float, t_up: float) -> None:
    gesture.event(FLAGS_CHANGED, keycode, True, t_down)
    gesture.event(FLAGS_CHANGED, keycode, False, t_up)


def _check() -> None:
    import tempfile

    assert pcm16([0.0, 1.0, -1.0]) == b"\x00\x00\xff\x7f\x01\x80"
    assert not worth_transcribing([0.0] * 100, SAMPLE_RATE)
    assert not worth_transcribing([0.0] * SAMPLE_RATE, SAMPLE_RATE)
    assert worth_transcribing([0.05] * SAMPLE_RATE, SAMPLE_RATE)
    clip = Path(tempfile.mkdtemp()) / "clip.wav"
    payload = pcm16([0.0, 1.0, -1.0])
    write_wav(clip, [0.0, 1.0, -1.0], SAMPLE_RATE)
    with wave.open(str(clip)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getnframes() == 3
        assert handle.readframes(3) == payload

    toggles: list[int] = []
    gesture = OptionDoubleTap(lambda: toggles.append(1))
    _press_option(gesture, 0x3A, 0.0, 0.1)
    assert toggles == []
    _press_option(gesture, 0x3A, 0.3, 0.4)
    assert toggles == [1]
    _press_option(gesture, 0x3A, 2.0, 2.1)
    _press_option(gesture, 0x3A, 3.0, 3.1)
    assert toggles == [1]
    gesture.event(FLAGS_CHANGED, 0x3A, True, 4.0)
    gesture.event(KEY_DOWN, 14, True, 4.05)
    gesture.event(FLAGS_CHANGED, 0x3A, False, 4.12)
    _press_option(gesture, 0x3A, 4.2, 4.3)
    assert toggles == [1]
    gesture.event(FLAGS_CHANGED, 0, True, 6.0)
    gesture.event(FLAGS_CHANGED, 0, False, 6.1)
    gesture.event(FLAGS_CHANGED, 0, True, 6.3)
    gesture.event(FLAGS_CHANGED, 0, False, 6.4)
    assert toggles == [1, 1]
    _press_option(gesture, 0x3A, 8.0, 8.1)
    _press_option(gesture, 0x3D, 8.25, 8.35)
    assert toggles == [1, 1, 1]

    paster = Paster()
    source = paster._cg.CGEventSourceCreate(HID_SOURCE)
    event = paster.command_v_event(source, True)
    if not source or not event:
        raise SystemExit("CoreGraphics did not create a paste keystroke")
    keycode = int(paster._cg.CGEventGetIntegerValueField(event, 9))
    flags = int(paster._cg.CGEventGetFlags(event))
    paster._cf.CFRelease(event)
    paster._cf.CFRelease(source)
    if keycode != V_KEYCODE or flags & COMMAND_FLAG != COMMAND_FLAG:
        raise SystemExit(
            f"paste keystroke was key {keycode} flags {flags:#x}")

    hotkeys = HotkeyTap(lambda: None, lambda: None)
    if not hotkeys.start():
        raise SystemExit("hotkey tap was not created; Input Monitoring is missing")
    _check_tap_run_loop(hotkeys)
    print("clip, paste keystroke, double-tap, hotkey tap: ok")


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--check":
        _check()
        return 0
    if len(argv) > 1 and argv[1] not in ("phonon-2", "phonon2"):
        raise SystemExit(
            "this app runs Phonon-2 on the Neural Engine.\n"
            f"unknown model: {argv[1]}")
    serve()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(130)
