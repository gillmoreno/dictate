# dictate

A personal Mac script for local dictation. Double-tap Option to record. Double-tap Option again, or press Escape, to stop. Phonon-2 transcribes the clip, the text is pasted where the cursor is, and the same text stays on the clipboard.

This is not a Fermion Research product. It only calls their installed tools. The repository does not include their code or the model weights.

## Install

The speech stack goes into a virtual environment in this folder. Python 3.11 is the version this was set up with.

```bash
python3.11 -m venv .venv
.venv/bin/pip install fermion-research
.venv/bin/pip install mlx mlx-audio mlx-lm soundfile sounddevice scipy zstandard
```

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

Wait for `ready. The microphone is off.` before the first double-tap. The first launch downloads the Phonon-2 weights.

macOS has to allow the app you launch it from (Terminal, iTerm, or similar) under Privacy & Security:

- Input Monitoring, so the Option double-tap is seen
- Accessibility, so the transcript can be pasted
- Microphone, so the clip can be recorded

The last recording is kept at `~/.cache/dictate/last.wav`.

## Their licenses

Sharing this script is allowed. It does not redistribute Fermion's work.

- The `fermion-research` package is Apache-2.0, copyright Fermion Research. Apache-2.0 allows use, modification, and sharing, including in a product you charge for, if you keep their license and notice when you distribute their code.
- Phonon-2 weights are CC-BY-4.0. They are derived from NVIDIA's `parakeet-tdt-0.6b-v3`, which is also CC-BY-4.0. Credit is required when you share the weights. The model card is [FermionResearch/Phonon-2](https://huggingface.co/FermionResearch/Phonon-2).

This script has no license of its own yet. People can read it. They should not assume they can reuse it.
