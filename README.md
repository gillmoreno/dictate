# dictate

A personal Mac script for local dictation. Double-tap Option to record. Double-tap Option again, or press Escape, to stop. Phonon-2 transcribes the clip on the Mac's Neural Engine, the text is pasted where the cursor is, and the same text stays on the clipboard.

This is not a Fermion Research product. The repository does not include their code or the model weights. The runner is a thin Swift program that links their [phonon-coreml](https://github.com/fermionresearch/phonon-coreml) package, and the weights download on first launch.

## Install

Python 3.11, Swift 6, and a Mac with Apple silicon on macOS 15 or later.

```bash
python3.11 -m venv .venv
.venv/bin/pip install sounddevice numpy huggingface_hub
```

The Neural Engine runner builds itself the first time you start dictation (`swift build -c release` in `engine/`). That needs a network once, to fetch the package.

## Run

With [just](https://github.com/casey/just) or make:

```bash
just dictate
make dictate
```

Both run the same thing, which also works on its own:

```bash
.venv/bin/python dictate.py
```

Wait for `ready. The microphone is off.` before the first double-tap. The first launch downloads the Phonon-2 Core ML weights (about 350 MB) and prepares them for this Mac's Neural Engine. That preparation can take several minutes. Later launches load in a few seconds. The model stays in its own process, off the GPU.

A Sennheiser microphone is used whenever one is connected. Otherwise the system default input records. Edit `PREFERRED_INPUTS` in `dictate.py` to prefer another microphone. A take that is too quiet is reported with its measured level and the threshold it needed.

macOS has to allow the app you launch it from (Terminal, iTerm, or similar) under Privacy & Security:

- Input Monitoring, so the Option double-tap is seen
- Accessibility, so the transcript can be pasted
- Microphone, so the clip can be recorded

The last recording is kept at `~/.cache/dictate/last.wav`. The model folder is `~/.cache/dictate/Phonon-2-CoreML`, and the Neural Engine runner is built in `~/.cache/dictate/engine-build`.

## Their licenses

Sharing this script is allowed. It does not redistribute Fermion's work.

- [phonon-coreml](https://github.com/fermionresearch/phonon-coreml) is Apache-2.0, copyright Fermion Research. It is fetched when the runner builds. Apache-2.0 allows use, modification, and sharing, including in a product you charge for, if you keep their license and notice when you distribute their code.
- The Phonon-2 Core ML weights are CC-BY-4.0. They are derived from NVIDIA's `parakeet-tdt-0.6b-v3`, which is also CC-BY-4.0. Credit is required when you share the weights. The model card is [FermionResearch/Phonon-2-CoreML](https://huggingface.co/FermionResearch/Phonon-2-CoreML).

This script has no license of its own yet. People can read it. They should not assume they can reuse it.
