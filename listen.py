#!/usr/bin/env python3
"""Live Phonon-2 listener. Says zoom-in / zoom-out to Stories.

Keeps the microphone open, transcribes a rolling 1.5s window, and pushes
matching commands to http://127.0.0.1:8765/events for the Stories page.
Ctrl-C quits.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dictate as d

LISTEN_PORT = 8095
WINDOW_S = 1.6
STRIDE_S = 0.8
COOLDOWN_S = 1.4
CLIP = Path.home() / ".cache" / "dictate" / "listen.wav"

clients: list[queue.Queue[str]] = []
clients_lock = threading.Lock()


def emit(command: str) -> None:
    with clients_lock:
        waiting = list(clients)
    for pending in waiting:
        try:
            pending.put_nowait(command)
        except queue.Full:
            pass


def command_of(text: str) -> str | None:
    words = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    if not words:
        return None
    if "zoom in" in words or "zooming in" in words:
        return "zoom-in"
    if "zoom out" in words or "zooming out" in words:
        return "zoom-out"
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/command":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8").strip()
        command = command_of(body) if body else None
        if command is None and body in ("zoom-in", "zoom-out"):
            command = body
        if not command:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        emit(command)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "command": command}).encode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}\n')
            return
        if self.path != "/events":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        pending: queue.Queue[str] = queue.Queue(maxsize=8)
        with clients_lock:
            clients.append(pending)
        try:
            self.wfile.write(b": ok\n\n")
            self.wfile.flush()
            while True:
                command = pending.get()
                self.wfile.write(f"data: {command}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with clients_lock:
                if pending in clients:
                    clients.remove(pending)


def serve_events() -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    thread = threading.Thread(target=httpd.serve_forever, name="listen-http", daemon=True)
    thread.start()
    return httpd


def live(speech: d.CoreMLSpeech) -> None:
    import numpy as np
    import sounddevice as sd

    blocks: queue.Queue = queue.Queue()
    window = int(WINDOW_S * d.SAMPLE_RATE)
    stride = int(STRIDE_S * d.SAMPLE_RATE)

    def on_audio(indata, frames, time_info, status):
        blocks.put(indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=d.SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=int(0.05 * d.SAMPLE_RATE),
        callback=on_audio,
    )
    stream.start()
    buf = np.zeros(0, dtype="float32")
    since = 0
    last_cmd = ""
    last_at = 0.0
    print(
        f"listening for zoom in / zoom out. Stories can join at http://127.0.0.1:{LISTEN_PORT}/events",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            try:
                chunk = blocks.get(timeout=0.25)
            except queue.Empty:
                continue
            buf = np.concatenate([buf, chunk])
            if buf.size > window:
                buf = buf[-window:]
            since += chunk.size
            if since < stride or buf.size < window:
                continue
            since = 0
            if not d.worth_transcribing(buf, d.SAMPLE_RATE):
                continue
            d.write_wav(CLIP, buf, d.SAMPLE_RATE)
            try:
                text, _, _ = speech.transcribe(CLIP)
            except Exception as exc:
                print(f"transcription failed ({exc}).", file=sys.stderr, flush=True)
                continue
            text = str(text).strip()
            if not text:
                continue
            print(f"heard: {text}", file=sys.stderr, flush=True)
            command = command_of(text)
            now = time.monotonic()
            if not command:
                continue
            if command == last_cmd and now - last_at < COOLDOWN_S:
                continue
            last_cmd = command
            last_at = now
            print(f"command: {command}", file=sys.stderr, flush=True)
            emit(command)
    finally:
        stream.stop()
        stream.close()


def main() -> None:
    d._use_mic_python()
    print("loading Phonon-2 on the Neural Engine…", file=sys.stderr, flush=True)
    speech = d.CoreMLSpeech()
    serve_events()
    try:
        live(speech)
    except KeyboardInterrupt:
        print("stopped.", file=sys.stderr, flush=True)
    finally:
        speech.close()


if __name__ == "__main__":
    main()
