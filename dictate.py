#!/usr/local/bin/python3.11
"""Type local Phonon-2 dictation into the focused app while you speak.

The microphone stays closed until you double-tap Option. Double-tap Option
again, or press Escape, to stop. Ctrl-C quits.

A short pause stays in the same phrase. Words appear after the guess has
settled, a little behind your voice, and a pause of about a second and a
half locks the phrase. Moving the cursor or typing yourself in the middle of
an unlocked phrase fights the correction, because the script can only delete
the characters it just inserted.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

DELETE_KEYCODE = 0x33
ESCAPE_KEYCODE = 0x35
HID_SOURCE = 1
HID_TAP = 0
KEY_DOWN = 10
KEY_UP = 11
FLAGS_CHANGED = 12
ALT_FLAG = 0x00080000
# Letters cancel a half-finished double-tap. These modifiers do not.
MODIFIER_KEYS = frozenset({
    0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
})
TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF)
MAX_HOLD_S = 0.50
DOUBLE_TAP_S = 0.60


def _fermion_python() -> str | None:
    """The interpreter that has the fermion package, if it is not this one."""
    candidates: list[str] = []
    binary = shutil.which("fermion")
    if binary:
        try:
            shebang = open(binary).readline().strip()
        except OSError:
            shebang = ""
        if shebang.startswith("#!"):
            candidates.append(shebang[2:].strip().split()[0])
    candidates.extend([
        "/usr/local/bin/python3.11",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11",
    ])
    mine = os.path.realpath(sys.executable)
    seen: set[str] = set()
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        real = os.path.realpath(path)
        if real in seen or real == mine:
            continue
        seen.add(real)
        return path
    return None


def _use_fermion_python() -> None:
    """Re-run under the Python that has Phonon when `python3` is another one."""
    try:
        import fermion  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if os.environ.get("DICTATE_FERMION_PY") == "1":
        raise SystemExit(
            "Phonon is not installed for this Python.\n"
            "The copy that works is: /usr/local/bin/python3.11 "
            "~/Desktop/utils/dictate/dictate.py")
    target = _fermion_python()
    if target is None:
        raise SystemExit(
            "Phonon is not installed for this Python, and `fermion` is not on PATH.\n"
            "The interpreter from `fermion listen` is /usr/local/bin/python3.11.")
    os.environ["DICTATE_FERMION_PY"] = "1"
    os.execv(target, [target, *sys.argv])


def revision(old: str, new: str) -> tuple[int, str]:
    """How to turn the text already inserted into the new guess.

    Returns the number of characters to delete and the suffix to insert.
    The shared prefix stays, so a growing sentence does not retype itself.
    """
    limit = min(len(old), len(new))
    kept = 0
    while kept < limit and old[kept] == new[kept]:
        kept += 1
    return len(old) - kept, new[kept:]


def _core(word: str) -> str:
    return word.strip(".,!?;:\"'“”").casefold()


def _common_cores(old: list[str], new: list[str]) -> int:
    count = 0
    for left, right in zip(old, new):
        if _core(left) != _core(right):
            break
        count += 1
    return count


class Typer:
    """Type a guess only after it has settled.

    The first glimpse is kept and not typed. Later glimpses type the words
    that stayed the same, except the last word, which is still being spoken.
    A finished phrase then adds that last word. Words already on screen are
    not deleted just because the next guess capitalizes them.
    """

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.shown: list[str] = []
        self.prev: list[str] | None = None

    def reset(self) -> None:
        self.shown = []
        self.prev = None

    def partial(self, text: str) -> None:
        words = text.split()
        if not words:
            return
        if self.prev is None:
            self.prev = words
            return
        common = _common_cores(self.prev, words)
        if not text.rstrip().endswith((".", "?", "!")) and common == len(words):
            common -= 1
        if common > 0:
            self._append(words[:common])
        self.prev = words

    def final(self, text: str) -> None:
        self._commit_rest(text.split())
        if self.shown:
            self.keyboard.insert(" ")
        self.reset()

    def flush(self) -> None:
        """Commit the latest guess, including the word still being held."""
        if self.prev:
            self._commit_rest(self.prev)
        if self.shown:
            self.keyboard.insert(" ")
        self.reset()

    def _append(self, words: list[str]) -> None:
        """Add words past what is shown. A partial never deletes."""
        if len(words) <= len(self.shown):
            return
        if _common_cores(self.shown, words) < len(self.shown):
            return
        extra = words[len(self.shown):]
        piece = (" " if self.shown else "") + " ".join(extra)
        self.keyboard.insert(piece)
        self.shown.extend(extra)

    def _commit_rest(self, words: list[str]) -> None:
        if not words:
            return
        match = _common_cores(self.shown, words)
        if match < len(self.shown):
            start = match
        elif self.shown and self.shown[-1] != words[len(self.shown) - 1]:
            start = len(self.shown) - 1
        else:
            start = len(self.shown)
        self._replace_from(start, words)

    def _replace_from(self, start: int, words: list[str]) -> None:
        if start < len(self.shown):
            tail = " ".join(self.shown[start:])
            if start > 0:
                tail = " " + tail
            self.keyboard.backspace(len(tail))
        extra = words[start:]
        if extra:
            piece = (" " if start > 0 else "") + " ".join(extra)
            self.keyboard.insert(piece)
        self.shown = list(words)


class Keyboard:
    """Post keystrokes through CoreGraphics. Needs Accessibility permission."""

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
        cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        cg.CGEventKeyboardGetUnicodeString.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._app.AXIsProcessTrusted.restype = ctypes.c_bool

    def trusted(self) -> bool:
        return bool(self._app.AXIsProcessTrusted())

    def insert(self, text: str) -> None:
        if not text:
            return
        encoded = text.encode("utf-16-le")
        count = len(encoded) // 2
        buf = (ctypes.c_uint16 * count).from_buffer_copy(encoded)
        self._post(0, ctypes.cast(buf, ctypes.c_void_p), count)

    def backspace(self, count: int) -> None:
        for _ in range(count):
            self._post(DELETE_KEYCODE, None, 0)

    def _post(self, keycode: int, utf16, count: int) -> None:
        source = self._cg.CGEventSourceCreate(HID_SOURCE)
        if not source:
            raise SystemExit("could not create a keyboard event source")
        try:
            for down in (True, False):
                event = self._cg.CGEventCreateKeyboardEvent(source, keycode, down)
                if not event:
                    raise SystemExit("could not create a keyboard event")
                if utf16 is not None:
                    self._cg.CGEventKeyboardSetUnicodeString(event, count, utf16)
                self._cg.CGEventPost(HID_TAP, event)
                self._cf.CFRelease(event)
        finally:
            self._cf.CFRelease(source)


def _announce(text: str) -> None:
    print(text, file=sys.stderr, flush=True)
    name = "Tink.aiff" if text.startswith("on") else "Pop.aiff"
    path = f"/System/Library/Sounds/{name}"
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
    """One microphone session at a time. Closed again as soon as it stops."""

    def __init__(self, speech, keyboard):
        self.speech = speech
        self.typer = Typer(keyboard)
        self.stop_mic = threading.Event()
        self.thread: threading.Thread | None = None
        self.on = False
        self._lock = threading.Lock()

    def toggle(self) -> None:
        with self._lock:
            if self.on:
                self.on = False
                self.stop_mic.set()
                _announce("off")
                return
            if self.thread is not None and self.thread.is_alive():
                return
            self.on = True
            self.stop_mic.clear()
            self.typer.reset()
            self.thread = threading.Thread(
                target=self._listen, name="dictate-mic", daemon=True)
            self.thread.start()
        _announce("on — speak")

    def stop(self) -> None:
        with self._lock:
            if not self.on:
                return
            self.on = False
            self.stop_mic.set()
        _announce("off")

    def _listen(self) -> None:
        from fermion._speech import live

        live.SILENCE_CLOSE_S = 1.5
        me = threading.current_thread()
        session = live.LiveSession(
            self.speech, on_partial=self._partial, on_final=self._final)
        try:
            _run_mic_until(session, self.stop_mic)
        except Exception as exc:
            print(f"microphone stopped ({exc}).", file=sys.stderr, flush=True)
        finally:
            with self._lock:
                if self.thread is me:
                    self.on = False

    def _partial(self, text: str) -> None:
        if self.stop_mic.is_set():
            return
        self.typer.partial(text)

    def _final(self, text: str) -> None:
        self.typer.final(text)


def _run_mic_until(session, stop_mic: threading.Event) -> None:
    import queue

    import sounddevice as sd
    from fermion._speech.engine import SAMPLE_RATE
    from fermion._speech.live import BLOCK_S

    blocks: "queue.Queue" = queue.Queue()

    def _on_audio(indata, frames, time_info, status):
        blocks.put(indata[:, 0].copy())

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=int(BLOCK_S * SAMPLE_RATE), callback=_on_audio)
        stream.start()
    except Exception as exc:
        print(
            f"could not open the microphone ({exc}). Grant this terminal "
            "microphone access in System Settings → Privacy & Security → "
            "Microphone, then double-tap Option again.",
            file=sys.stderr, flush=True)
        return
    try:
        while not stop_mic.is_set():
            try:
                session.feed(blocks.get(timeout=0.25))
            except queue.Empty:
                continue
            while not stop_mic.is_set():
                try:
                    session.feed(blocks.get_nowait())
                except queue.Empty:
                    break
    finally:
        stream.stop()
        stream.close()
    session.finish()


def load_model(model: str):
    from fermion._speech import backends, fetch, live
    from fermion.transcribe import _resolve

    repo, key, pin, local_dir = _resolve(model)
    model_dir = local_dir if local_dir is not None else fetch.ensure(repo, key, pin)
    speech = backends.load(
        "mlx", model_dir, profile=key, backend=pin["backend"], quiet=True)
    print("warming the model (first launch in a while compiles shaders)…",
          file=sys.stderr, flush=True)
    live.warm_up(speech)
    return speech


def serve(model: str) -> None:
    from fermion._speech import gate, live

    keyboard = Keyboard()
    if not keyboard.trusted():
        raise SystemExit(
            "this terminal cannot type into other apps yet.\n"
            "System Settings → Privacy & Security → Accessibility, enable the "
            "app you launched this from (Terminal, iTerm, or Grok), then run it again.\n"
            f"interpreter: {sys.executable}")
    gate.require_listen("`dictate`")
    print("loading Phonon-2 on this Mac…", file=sys.stderr, flush=True)
    speech = load_model(model)
    # A pause of 0.7 s used to end the phrase. The finished decode then
    # replaced the line, which reads as the sentence starting over.
    live.SILENCE_CLOSE_S = 1.5
    dictation = Dictation(speech, keyboard)
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
    print("Double-tap Option to dictate. Double-tap again, or press Escape, to stop.",
          file=sys.stderr, flush=True)
    print("Ctrl-C quits.", file=sys.stderr, flush=True)
    while True:
        hotkeys.pump(0.25)


class _Rec:
    def __init__(self):
        self.text = ""

    def insert(self, text: str) -> None:
        self.text += text

    def backspace(self, count: int) -> None:
        if count:
            self.text = self.text[:-count]


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
    assert revision("", "Hello") == (0, "Hello")
    assert revision("Hello", "Hello there") == (0, " there")
    assert revision("hell is", "what is") == (7, "what is")

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

    keyboard = Keyboard()
    source = keyboard._cg.CGEventSourceCreate(HID_SOURCE)
    event = keyboard._cg.CGEventCreateKeyboardEvent(source, 0, True)
    if not source or not event:
        raise SystemExit("CoreGraphics did not create a keyboard event")
    sample = "Hello"
    raw = sample.encode("utf-16-le")
    buf = (ctypes.c_uint16 * len(sample)).from_buffer_copy(raw)
    keyboard._cg.CGEventKeyboardSetUnicodeString(
        event, len(sample), ctypes.cast(buf, ctypes.c_void_p))
    got_n = ctypes.c_ulong()
    got = (ctypes.c_uint16 * len(sample))()
    keyboard._cg.CGEventKeyboardGetUnicodeString(
        event, len(sample), ctypes.byref(got_n), ctypes.cast(got, ctypes.c_void_p))
    read = bytes(got).decode("utf-16-le")
    keyboard._cf.CFRelease(event)
    keyboard._cf.CFRelease(source)
    if got_n.value != len(sample) or read != sample:
        raise SystemExit(f"keyboard event returned {read!r}, expected {sample!r}")
    steady = _Rec()
    typer = Typer(steady)
    typer.partial("it is working")
    assert steady.text == ""
    typer.partial("it is working now")
    assert steady.text == "it is working"
    typer.partial("this is working now please")
    assert steady.text == "it is working"
    typer.final("It is working now.")
    assert steady.text == "it is working now. "
    hotkeys = HotkeyTap(lambda: None, lambda: None)
    if not hotkeys.start():
        raise SystemExit("hotkey tap was not created; Input Monitoring is missing")
    _check_tap_run_loop(hotkeys)
    print("double-tap, steady typing, hotkey tap: ok")
    print("accessibility:", "granted" if keyboard.trusted() else "not granted")


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--check":
        _check()
        return 0
    _use_fermion_python()
    model = argv[1] if len(argv) > 1 else "phonon-2"
    serve(model)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        raise SystemExit(130)
