# Local Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local Whisper-based transcription with speaker diarization (Daniel/Maya) to podpub, surface transcripts via `<podcast:transcript>` RSS tags, and backfill the 7 existing episodes.

**Architecture:** New `transcribe.py` module runs WhisperX (faster-whisper `large-v3` + wav2vec2 alignment + pyannote diarization), classifies speakers by F0, and emits VTT. `podpub.py` imports it and invokes during publish. Feed XML gains Podcasting 2.0 namespace + `<podcast:transcript>` tags via ElementTree post-processing of the feedgen output.

**Tech Stack:** Python 3.12, WhisperX, PyTorch, pyannote.audio, librosa, feedgen (existing), ElementTree (stdlib).

**Reference:** `docs/superpowers/specs/2026-04-24-local-transcription-design.md`

**Verification model:** This is a personal publishing tool with no pytest suite (per spec). Each task verifies by running a real command against real inputs and inspecting the output. The canonical acceptance test is "transcribe episode 001, spot-check the VTT, confirm Apple Podcasts renders it." Every task ends with a commit before the next begins.

**Working directory for all commands:** `/Users/msoltani/Library/CloudStorage/GoogleDrive-msoltani@bu.edu/My Drive/_03_Projects/podpub`. All `git`, `python`, and file paths assume this is `$PWD`. Wrap in double-quotes when `cd`ing.

---

## Task 1: Project scaffolding — deps, transcripts folder, config template

**Files:**
- Modify: `setup/requirements.txt`
- Modify: `setup/config.yaml.example`
- Create: `transcripts/.gitkeep`

- [ ] **Step 1.1: Append transcription dependencies to `setup/requirements.txt`**

Replace file contents with:

```
feedgen>=1.0.0
PyYAML>=6.0

# Transcription (local, no cloud APIs)
whisperx>=3.2.0
torch>=2.0,<2.4
torchaudio>=2.0,<2.4
librosa>=0.10
numpy>=1.24
```

Note: exact WhisperX + torch pins are intentionally loose. After the deps install cleanly in step 1.3, we'll pin the resolved versions in step 1.5.

- [ ] **Step 1.2: Create `transcripts/` folder with a `.gitkeep`**

```bash
mkdir -p transcripts
touch transcripts/.gitkeep
```

- [ ] **Step 1.3: Install deps into the existing venv**

```bash
.venv/bin/pip install -r setup/requirements.txt
```

Expected: no errors. WhisperX may take 3–5 minutes; it pulls a lot of transitive deps (torch, pyannote, etc). If this fails on Apple Silicon with a torch wheel error, drop the torch pin and retry (`pip install torch torchaudio` then `pip install whisperx librosa`).

- [ ] **Step 1.4: Smoke-test imports**

```bash
.venv/bin/python -c "import whisperx, torch, torchaudio, librosa; print(whisperx.__version__, torch.__version__)"
```

Expected: two version strings, no traceback.

- [ ] **Step 1.5: Pin resolved versions in `setup/requirements.txt`**

Capture resolved versions:

```bash
.venv/bin/pip show whisperx torch torchaudio librosa numpy | grep -E "^(Name|Version)"
```

Update `setup/requirements.txt` to pin exact versions (replace the loose pins):

```
feedgen>=1.0.0
PyYAML>=6.0

# Transcription (local, no cloud APIs)
# Pinned to versions verified on macOS 14+ / Apple Silicon / Python 3.12.
whisperx==<RESOLVED>
torch==<RESOLVED>
torchaudio==<RESOLVED>
librosa==<RESOLVED>
numpy==<RESOLVED>
```

Substitute the actual resolved versions from `pip show`. This step cements the "known-good" pins for future environment rebuilds.

- [ ] **Step 1.6: Append transcription block to `setup/config.yaml.example`**

Append to the existing file:

```yaml

transcription:
  model: large-v3
  language: en
  # Get a token from https://huggingface.co/settings/tokens (free, read scope).
  # Also accept the licenses on these two models before first run:
  #   https://huggingface.co/pyannote/speaker-diarization-3.1
  #   https://huggingface.co/pyannote/segmentation-3.0
  hf_token: hf_REPLACE_ME
  male_label: Daniel
  female_label: Maya
  # Below this fundamental frequency the diarized speaker is labeled as the
  # male voice; above, as the female voice. 165 Hz is a clean midpoint
  # between typical male (85-155) and female (165-255) F0 ranges.
  f0_threshold_hz: 165
```

- [ ] **Step 1.7: Commit**

```bash
git add setup/requirements.txt setup/config.yaml.example transcripts/.gitkeep
git commit -m "Add transcription dependencies and transcripts/ folder"
```

---

## Task 2: Document HuggingFace setup in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

This is a documentation-only task, but it's on the critical path — without the user completing the HF setup, nothing transcribes.

- [ ] **Step 2.1: Add a "Transcription setup" section to `CLAUDE.md`**

Insert this section immediately before the "One-time setup" section (which starts with `## One-time setup`):

```markdown
## Transcription setup (one-time, per machine)

Episode transcripts are generated locally by `transcribe.py` using WhisperX
(faster-whisper `large-v3`) and pyannote.audio for speaker diarization. Both
run fully offline once the model weights are downloaded. Speakers are labeled
**Daniel** (male voice) and **Maya** (female voice).

To enable transcription on a fresh machine:

1. **Create a HuggingFace account** (free): https://huggingface.co/join
2. **Accept the license** on each of these two models (click through once):
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0
3. **Generate a read-scope access token** at
   https://huggingface.co/settings/tokens. Copy it.
4. **Paste the token** into `config.yaml` under `transcription.hf_token`
   (replace `hf_REPLACE_ME`). This file is gitignored — the token never leaves
   your machine.
5. **First transcription run** downloads ~3 GB of model weights to
   `~/.cache/huggingface/`. Subsequent runs are offline.

If the token is missing or the licenses are not accepted, `transcribe.py`
fails fast with a pointer to the URLs above.

```

- [ ] **Step 2.2: Add transcription workflow notes to the Publishing workflow section**

In `CLAUDE.md`, find the "## Publishing workflow" section. Extend step 3 ("Preview, then publish") so it reads:

```markdown
3. **Preview, then publish.**
   - First run: `.venv/bin/python podpub.py --dry-run` — sanity-check the rename plan, feed XML, and commit message.
   - If everything looks right, run: `.venv/bin/python podpub.py` — this transcribes each new episode (takes 1–2 min per episode), moves files into `audio/` and `transcripts/`, rebuilds `feed.xml`, commits, and pushes to `origin/main`. GitHub Pages auto-deploys within ~30 seconds.
   - To publish without transcribing (e.g., transcription tooling is broken): add `--skip-transcripts`.
```

And extend the "Commands reference" section with:

```markdown
- `.venv/bin/python podpub.py --backfill-transcripts` — generate VTTs for existing episodes that don't have one, inject transcript URLs into `feed.xml`, commit, and push.
- `.venv/bin/python podpub.py --skip-transcripts` — publish without generating transcripts for new episodes.
- `.venv/bin/python transcribe.py <audio_file> [--output <vtt>] [--force]` — standalone transcriber; useful for one-off re-transcription.
```

- [ ] **Step 2.3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document HuggingFace setup and transcription workflow in CLAUDE.md"
```

---

## Task 3: User completes HuggingFace setup (manual)

**Files:** None (user's local `config.yaml`, gitignored).

This is a gate, not a code task. Implementation cannot proceed past Task 6 without it.

- [ ] **Step 3.1: Confirm the user has:**
   1. Accepted the license on `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0` on HuggingFace.
   2. Generated a read-scope HF token and pasted it into `config.yaml` under `transcription.hf_token`.

If not, pause here and walk through the steps from `CLAUDE.md`. Do NOT proceed with code that depends on the token until this is done.

- [ ] **Step 3.2: Verify config has the transcription section**

```bash
.venv/bin/python -c "import yaml; c=yaml.safe_load(open('config.yaml')); t=c.get('transcription'); assert t and t.get('hf_token','').startswith('hf_') and t['hf_token'] != 'hf_REPLACE_ME', 'transcription.hf_token missing or unchanged'; print('config OK:', t['model'], t['male_label'], t['female_label'])"
```

Expected: `config OK: large-v3 Daniel Maya`. If not, fix `config.yaml` and re-run.

---

## Task 4: `transcribe.py` — audio loading, ASR, and alignment

**Files:**
- Create: `transcribe.py`

We build `transcribe.py` incrementally. At the end of Task 4 it runs transcription + alignment only (no diarization yet), prints the result, and exits — enough to verify Whisper is working on real audio before layering on diarization.

- [ ] **Step 4.1: Create the file skeleton with imports and config loading**

Create `transcribe.py` with exactly this content:

```python
#!/usr/bin/env python3
"""transcribe.py — generate VTT transcripts with speaker diarization.

Usage:
  python transcribe.py <audio_file> [--output <vtt_path>] [--force]

Runs fully local. Requires config.yaml with a transcription.hf_token that has
accepted the pyannote license. First run downloads ~3GB of model weights.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

log = logging.getLogger("transcribe")


@dataclass
class TranscriptionResult:
    num_cues: int
    duration_sec: float
    daniel_ratio: float
    maya_ratio: float
    model: str


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {CONFIG_FILE}. Run podpub.py once to create it."
        )
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    t = cfg.get("transcription")
    if not t:
        raise ValueError(
            "config.yaml is missing the 'transcription' section. "
            "See setup/config.yaml.example."
        )
    token = t.get("hf_token", "")
    if not token or token == "hf_REPLACE_ME":
        raise ValueError(
            "config.yaml transcription.hf_token is not set. "
            "Accept licenses on pyannote/speaker-diarization-3.1 and "
            "pyannote/segmentation-3.0, then paste your HF token."
        )
    return cfg


def _transcribe_and_align(audio_path: Path, cfg: dict) -> tuple[dict, np.ndarray]:
    """Returns (aligned_result, raw_audio_16k_mono)."""
    import whisperx  # imported lazily so --help is instant

    t = cfg["transcription"]
    device = "cpu"  # Apple Silicon: stable on CPU; MPS has partial coverage
    compute_type = "int8"

    log.info("loading audio: %s", audio_path)
    audio = whisperx.load_audio(str(audio_path))

    log.info("loading whisper model: %s", t["model"])
    model = whisperx.load_model(
        t["model"], device, compute_type=compute_type, language=t["language"]
    )

    log.info("transcribing (%.1fs of audio)", len(audio) / 16000)
    result = model.transcribe(audio, batch_size=16)

    log.info("loading alignment model")
    align_model, align_metadata = whisperx.load_align_model(
        language_code=t["language"], device=device
    )

    log.info("aligning")
    result = whisperx.align(
        result["segments"], align_model, align_metadata, audio, device,
        return_char_alignments=False,
    )
    return result, audio
```

- [ ] **Step 4.2: Run the script as a smoke test (expected to fail with "no main yet")**

```bash
.venv/bin/python transcribe.py
```

Expected: an error from argparse or a syntax/import failure — we haven't added the CLI yet. The point is to verify the file imports without errors.

Actually at this stage there's no `if __name__ == "__main__"` block, so running the file is a no-op. Verify imports instead:

```bash
.venv/bin/python -c "import transcribe; print('imports ok')"
```

Expected: `imports ok` and no traceback.

- [ ] **Step 4.3: Add a temporary `__main__` block for manual verification**

Append to `transcribe.py`:

```python


if __name__ == "__main__":
    # Temporary: verify ASR+alignment on a real audio file.
    # This block will be replaced by the real CLI in Task 7.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    audio_arg = sys.argv[1] if len(sys.argv) > 1 else "audio/001 - Why AI Has A Body Problem.m4a"
    cfg = load_config()
    result, _audio = _transcribe_and_align(Path(audio_arg), cfg)
    segments = result["segments"]
    print(f"\n{len(segments)} segments, first 3:\n")
    for s in segments[:3]:
        words = s.get("words", [])
        print(f"  [{s['start']:.2f}-{s['end']:.2f}] {s['text'][:80]}")
        print(f"    ({len(words)} word timestamps, first: {words[0] if words else 'N/A'})")
```

- [ ] **Step 4.4: Run ASR+alignment on episode 001**

```bash
.venv/bin/python transcribe.py "audio/001 - Why AI Has A Body Problem.m4a"
```

Expected:
- First run downloads the WhisperX large-v3 model (~2.9 GB) + wav2vec2 alignment model (~1.3 GB). This takes several minutes on first run; cached thereafter.
- Logs: "loading audio", "loading whisper model", "transcribing", "loading alignment model", "aligning".
- Prints `<N> segments, first 3:` where N is around 30–80 for a 5-minute episode.
- Each printed segment shows text + word-count + first word with `start`/`end`/`word` keys.
- Total runtime (after models cached): under 90 seconds on M1 Max.

If you see a tokenizer or decoder error, it's usually a torch/whisperx version mismatch — revisit step 1.5.

- [ ] **Step 4.5: Commit**

```bash
git add transcribe.py
git commit -m "Add transcribe.py ASR+alignment scaffold (no diarization yet)"
```

---

## Task 5: `transcribe.py` — diarization and word-speaker assignment

**Files:**
- Modify: `transcribe.py`

- [ ] **Step 5.1: Add the diarization function**

Insert this function into `transcribe.py` **after** `_transcribe_and_align` and **before** the `if __name__ == "__main__"` block:

```python
def _diarize(audio: np.ndarray, result: dict, cfg: dict) -> dict:
    """Diarize the audio and assign a speaker label to each word.

    Returns the aligned result mutated to include `speaker` on each word and
    on each segment. Speaker labels are pyannote-native (SPEAKER_00 / SPEAKER_01).
    """
    import whisperx
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    t = cfg["transcription"]
    log.info("loading diarization pipeline")
    pipeline = DiarizationPipeline(use_auth_token=t["hf_token"], device="cpu")

    log.info("diarizing (forced num_speakers=2)")
    diarize_segments = pipeline(audio, num_speakers=2)

    log.info("assigning speakers to words")
    result = assign_word_speakers(diarize_segments, result)

    # Count speakers seen
    speakers = set()
    for seg in result["segments"]:
        if "speaker" in seg:
            speakers.add(seg["speaker"])
    log.info("diarization found speakers: %s", sorted(speakers))
    return result
```

Note: WhisperX's diarization module path varies by version. If `from whisperx.diarize import DiarizationPipeline, assign_word_speakers` fails, try `from whisperx import DiarizationPipeline, assign_word_speakers` — both exist in different releases. Pick whichever imports successfully and keep the version pinned.

- [ ] **Step 5.2: Update the temporary `__main__` block to run diarization too**

Replace the `if __name__ == "__main__"` block (from Step 4.3) with:

```python


if __name__ == "__main__":
    # Temporary: verify ASR + alignment + diarization end-to-end.
    # This block will be replaced by the real CLI in Task 7.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    audio_arg = sys.argv[1] if len(sys.argv) > 1 else "audio/001 - Why AI Has A Body Problem.m4a"
    cfg = load_config()
    result, audio = _transcribe_and_align(Path(audio_arg), cfg)
    result = _diarize(audio, result, cfg)
    segments = result["segments"]
    print(f"\n{len(segments)} segments, first 5 with speakers:\n")
    for s in segments[:5]:
        spk = s.get("speaker", "UNKNOWN")
        print(f"  [{spk}] [{s['start']:.2f}-{s['end']:.2f}] {s['text'][:80]}")
```

- [ ] **Step 5.3: Run end-to-end on episode 001**

```bash
.venv/bin/python transcribe.py "audio/001 - Why AI Has A Body Problem.m4a"
```

Expected:
- First run downloads pyannote models (~200 MB). Cached thereafter.
- Log: `diarization found speakers: ['SPEAKER_00', 'SPEAKER_01']`.
- First 5 segments print with `[SPEAKER_00]` or `[SPEAKER_01]` labels that alternate (the two NotebookLM hosts trade turns).
- Total runtime: ~90–120 seconds on M1 Max (after caches warm).

If you see "unauthorized" or "cannot load model" from pyannote, the HF token / licenses are wrong — see CLAUDE.md transcription setup.

- [ ] **Step 5.4: Commit**

```bash
git add transcribe.py
git commit -m "Add pyannote diarization + word-speaker assignment to transcribe.py"
```

---

## Task 6: `transcribe.py` — Daniel/Maya label assignment via F0

**Files:**
- Modify: `transcribe.py`

- [ ] **Step 6.1: Add the F0 classification function**

Insert this after `_diarize` and before the `__main__` block:

```python
def _build_speaker_map(audio: np.ndarray, result: dict, cfg: dict) -> dict[str, str]:
    """Map pyannote speaker IDs (SPEAKER_00, SPEAKER_01) to Daniel/Maya based on F0.

    For each speaker cluster, concatenate the first ~3 seconds of cumulative
    speech from that cluster and take the median fundamental frequency.
    Below `f0_threshold_hz` → male_label; above → female_label.

    If only one speaker is detected, maps it to male_label (arbitrary but
    stable; a warning is logged upstream).

    If both clusters fall on the same side of the threshold (unusual), the
    one with the lower F0 becomes male_label and the other becomes female_label
    so we always produce two distinct labels.
    """
    import librosa

    t = cfg["transcription"]
    male = t["male_label"]
    female = t["female_label"]
    threshold = float(t["f0_threshold_hz"])
    sr = 16000  # whisperx.load_audio returns 16k mono

    # Collect word-level (start, end, speaker) tuples
    per_speaker_clips: dict[str, list[tuple[float, float]]] = {}
    for seg in result["segments"]:
        for w in seg.get("words", []):
            spk = w.get("speaker")
            if spk is None:
                continue
            if "start" not in w or "end" not in w:
                continue
            per_speaker_clips.setdefault(spk, []).append((w["start"], w["end"]))

    if not per_speaker_clips:
        log.warning("no word-level speaker info found; returning empty map")
        return {}

    if len(per_speaker_clips) == 1:
        only = next(iter(per_speaker_clips))
        log.warning("only one speaker cluster found; labeling as %s", male)
        return {only: male}

    # For each speaker, concatenate audio up to ~3 cumulative seconds and
    # measure median F0.
    speaker_f0: dict[str, float] = {}
    for spk, intervals in per_speaker_clips.items():
        intervals.sort()
        collected: list[np.ndarray] = []
        total = 0.0
        for start, end in intervals:
            if total >= 3.0:
                break
            s_idx = int(start * sr)
            e_idx = int(end * sr)
            if e_idx <= s_idx:
                continue
            collected.append(audio[s_idx:e_idx])
            total += end - start
        if not collected:
            speaker_f0[spk] = float("nan")
            continue
        clip = np.concatenate(collected)
        f0, voiced_flag, _ = librosa.pyin(
            clip,
            fmin=float(librosa.note_to_hz("C2")),   # ~65 Hz
            fmax=float(librosa.note_to_hz("C6")),   # ~1047 Hz
            sr=sr,
        )
        voiced = f0[voiced_flag]
        voiced = voiced[~np.isnan(voiced)]
        speaker_f0[spk] = float(np.median(voiced)) if len(voiced) else float("nan")

    log.info("median F0 per cluster: %s", {k: round(v, 1) for k, v in speaker_f0.items()})

    spk_sorted = sorted(speaker_f0.items(), key=lambda kv: (float("inf") if np.isnan(kv[1]) else kv[1]))
    # Lower F0 → male; higher → female. Guarantees two distinct labels.
    low_spk, low_f0 = spk_sorted[0]
    high_spk, high_f0 = spk_sorted[-1]
    mapping = {low_spk: male, high_spk: female}

    # Sanity check: if the "low" cluster is above the threshold, log a warning —
    # both voices look high. Mapping is still consistent (lower→male, higher→female).
    if not np.isnan(low_f0) and low_f0 > threshold:
        log.warning(
            "both clusters have F0 > %.0f Hz (%s=%.1f, %s=%.1f); "
            "using relative pitch for mapping",
            threshold, low_spk, low_f0, high_spk, high_f0,
        )
    return mapping
```

- [ ] **Step 6.2: Update the temporary `__main__` block to print the speaker map**

Replace the `if __name__ == "__main__"` block with:

```python


if __name__ == "__main__":
    # Temporary: verify ASR + diarization + speaker mapping.
    # This block will be replaced by the real CLI in Task 7.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    audio_arg = sys.argv[1] if len(sys.argv) > 1 else "audio/001 - Why AI Has A Body Problem.m4a"
    cfg = load_config()
    result, audio = _transcribe_and_align(Path(audio_arg), cfg)
    result = _diarize(audio, result, cfg)
    speaker_map = _build_speaker_map(audio, result, cfg)
    print(f"\nSpeaker map: {speaker_map}\n")
    print("First 5 segments with named speakers:\n")
    for s in result["segments"][:5]:
        raw_spk = s.get("speaker", "UNKNOWN")
        named = speaker_map.get(raw_spk, raw_spk)
        print(f"  [{named}] [{s['start']:.2f}-{s['end']:.2f}] {s['text'][:80]}")
```

- [ ] **Step 6.3: Run on episode 001 and verify the labels match the voices**

```bash
.venv/bin/python transcribe.py "audio/001 - Why AI Has A Body Problem.m4a"
```

Expected:
- Log includes `median F0 per cluster: {'SPEAKER_00': <xx>.x, 'SPEAKER_01': <yy>.y}`. One should be low (80–140 Hz range for the NotebookLM male voice), the other high (180–220 Hz range for the female voice).
- Printed speaker map looks like `{'SPEAKER_01': 'Daniel', 'SPEAKER_00': 'Maya'}` (order may vary).
- First 5 segments show `[Daniel]` or `[Maya]` labels.

**Manual ear-check (required before commit):** Open `audio/001 - Why AI Has A Body Problem.m4a` in any audio player. Jump to the first segment timestamp printed. If the label says `[Daniel]`, the voice should be male; `[Maya]` should be female. If they're swapped, revisit: likely the F0 extraction returned `NaN` for one cluster (check the log). If the mapping is correct, proceed.

- [ ] **Step 6.4: Commit**

```bash
git add transcribe.py
git commit -m "Map diarized clusters to Daniel/Maya via F0 classification"
```

---

## Task 7: `transcribe.py` — VTT emission + public API + CLI

**Files:**
- Modify: `transcribe.py`

This is the final shape of `transcribe.py`.

- [ ] **Step 7.1: Add cue-collapse and VTT-writer helpers**

Insert after `_build_speaker_map` and before the `__main__` block:

```python
def _collapse_to_cues(result: dict, speaker_map: dict[str, str], max_cue_sec: float = 15.0) -> list[dict]:
    """Group consecutive same-speaker words into cues, breaking on speaker
    change or when cue duration exceeds max_cue_sec.

    Returns list of cues: {"start": float, "end": float, "speaker": str, "text": str}
    where `speaker` is already the mapped name (Daniel/Maya) or the raw
    SPEAKER_XX string if not in the map.
    """
    cues: list[dict] = []
    current: dict | None = None
    for seg in result["segments"]:
        for w in seg.get("words", []):
            if "start" not in w or "end" not in w:
                continue
            raw_spk = w.get("speaker")
            spk_name = speaker_map.get(raw_spk, raw_spk or "")
            word_text = w.get("word", "")
            if current is None:
                current = {"start": w["start"], "end": w["end"],
                           "speaker": spk_name, "text": word_text}
                continue
            same_speaker = current["speaker"] == spk_name
            within_limit = (w["end"] - current["start"]) <= max_cue_sec
            if same_speaker and within_limit:
                current["end"] = w["end"]
                current["text"] += word_text
            else:
                cues.append(current)
                current = {"start": w["start"], "end": w["end"],
                           "speaker": spk_name, "text": word_text}
    if current is not None:
        cues.append(current)
    # Trim leading/trailing whitespace on text
    for c in cues:
        c["text"] = c["text"].strip()
    return cues


def _format_vtt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - (h * 3600) - (m * 60)
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _write_vtt(cues: list[dict], output_path: Path) -> None:
    lines = ["WEBVTT", ""]
    for i, c in enumerate(cues, start=1):
        start = _format_vtt_time(c["start"])
        end = _format_vtt_time(c["end"])
        text = c["text"]
        spk = c["speaker"]
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        if spk and not spk.startswith("SPEAKER_"):
            lines.append(f"<v {spk}>{text}</v>")
        else:
            # Unmapped or no speaker: emit plain text
            lines.append(text)
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 7.2: Add the public `transcribe_audio` function**

Insert after `_write_vtt`:

```python
def transcribe_audio(
    audio_path: Path,
    output_vtt_path: Path,
    cfg: dict,
    *,
    force: bool = False,
) -> TranscriptionResult:
    """Transcribe one audio file to VTT with Daniel/Maya speaker labels.

    Raises FileExistsError if output_vtt_path exists and force=False.
    Raises FileNotFoundError if audio_path doesn't exist.
    Any downstream failure (corrupt audio, missing HF license) propagates.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")
    if output_vtt_path.exists() and not force:
        raise FileExistsError(
            f"{output_vtt_path} already exists. Pass force=True (or --force) to overwrite."
        )
    output_vtt_path.parent.mkdir(parents=True, exist_ok=True)

    result, audio = _transcribe_and_align(audio_path, cfg)
    result = _diarize(audio, result, cfg)
    speaker_map = _build_speaker_map(audio, result, cfg)
    cues = _collapse_to_cues(result, speaker_map)
    _write_vtt(cues, output_vtt_path)

    duration = float(len(audio)) / 16000.0
    male = cfg["transcription"]["male_label"]
    female = cfg["transcription"]["female_label"]
    male_time = sum(c["end"] - c["start"] for c in cues if c["speaker"] == male)
    female_time = sum(c["end"] - c["start"] for c in cues if c["speaker"] == female)
    total_speech = male_time + female_time
    male_ratio = (male_time / total_speech) if total_speech > 0 else 0.0
    female_ratio = (female_time / total_speech) if total_speech > 0 else 0.0

    log.info(
        "%s: %d cues, %s (%.0f%%) + %s (%.0f%%), duration %.1fs",
        output_vtt_path.name, len(cues),
        male, male_ratio * 100, female, female_ratio * 100, duration,
    )
    if total_speech > 0 and (male_ratio < 0.20 or male_ratio > 0.80):
        log.warning(
            "speaker ratio looks skewed (%s %.0f%% / %s %.0f%%); "
            "diarization may be suspect", male, male_ratio * 100, female, female_ratio * 100,
        )

    return TranscriptionResult(
        num_cues=len(cues),
        duration_sec=duration,
        daniel_ratio=male_ratio,
        maya_ratio=female_ratio,
        model=cfg["transcription"]["model"],
    )
```

- [ ] **Step 7.3: Replace the temporary `__main__` block with the real CLI**

Replace the existing `if __name__ == "__main__":` block with:

```python


def _main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe an audio file to VTT with speaker diarization.")
    ap.add_argument("audio_path", help="Path to audio file (.m4a, .mp3, .wav)")
    ap.add_argument("--output", help="Path to output .vtt (default: sibling of audio_path)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing VTT")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    audio_path = Path(args.audio_path)
    output_path = Path(args.output) if args.output else audio_path.with_suffix(".vtt")

    cfg = load_config()
    try:
        result = transcribe_audio(audio_path, output_path, cfg, force=args.force)
    except FileExistsError as e:
        log.error(str(e))
        return 2
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    log.info("wrote: %s", output_path)
    log.info("result: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

- [ ] **Step 7.4: Run the CLI end-to-end on episode 001**

```bash
.venv/bin/python transcribe.py "audio/001 - Why AI Has A Body Problem.m4a" --output /tmp/001.vtt --force
```

Expected:
- Log: `wrote: /tmp/001.vtt` + `result: TranscriptionResult(num_cues=<N>, duration_sec=<D>, daniel_ratio=<r1>, maya_ratio=<r2>, model='large-v3')`.
- `<N>` typically 80–150 for a 5-minute NotebookLM episode.
- `<r1> + <r2>` ≈ 1.0 (small gaps for silence).
- Neither ratio <0.20 or >0.80 (otherwise there's a diarization issue).

- [ ] **Step 7.5: Inspect the VTT**

```bash
head -40 /tmp/001.vtt
```

Expected: starts with `WEBVTT` blank-line-then-cue-1. Each cue is:
```
1
00:00:00.480 --> 00:00:05.220
<v Maya>Welcome back. Today we're diving into...</v>

```
- Speaker labels alternate between `<v Daniel>` and `<v Maya>`.
- Timestamps are `HH:MM:SS.mmm`.
- Text reads cleanly.

**Required ear-check:** open episode 001 in an audio player, jump to the timestamp of the first 2–3 cues, confirm the voice matches the `<v ...>` label. If labels are swapped, revisit Task 6 — most likely cause is near-threshold F0 on both voices; the fallback relative-pitch mapping should still label consistently but the male/female assignment could be wrong if both voices happen to be high-pitched (not the case for NotebookLM).

- [ ] **Step 7.6: Commit**

```bash
git add transcribe.py
git commit -m "Wire up transcribe.py: cues, VTT output, public API, CLI"
```

---

## Task 8: `podpub.py` — extend feed parsing and building for transcripts

**Files:**
- Modify: `podpub.py`

We extend `parse_existing_feed` to read existing `<podcast:transcript>` URLs, and extend `build_feed` to inject the Podcasting 2.0 namespace + a `<podcast:transcript>` child on items that have a transcript. Nothing in `podpub.py` yet calls the transcriber — that's Tasks 10 and 11.

- [ ] **Step 8.1: Add the Podcasting 2.0 namespace constant**

In `podpub.py`, find the block of module-level constants near the top (after `SUPPORTED_EXTS`). Add `PODCAST_NS` alongside `ITUNES_NS`:

```python
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
```

- [ ] **Step 8.2: Extend `parse_existing_feed` to read transcript URLs**

Find `parse_existing_feed` in `podpub.py`. Update the namespace dict and the item-dict population to include the transcript URL. Replace:

```python
    ns = {"itunes": ITUNES_NS}
```

with:

```python
    ns = {"itunes": ITUNES_NS, "podcast": PODCAST_NS}
```

Then in the `for el in channel.findall("item"):` loop, replace the `items.append({...})` block with:

```python
        transcript_el = el.find("podcast:transcript", namespaces=ns)
        transcript_url = transcript_el.get("url") if transcript_el is not None else ""
        items.append({
            "title": _strip_title_prefix(el.findtext("title", "")),
            "guid": el.findtext("guid", ""),
            "pub_date": el.findtext("pubDate", ""),
            "description": el.findtext("description", ""),
            "enclosure_url": enc.get("url", "") if enc is not None else "",
            "enclosure_length": enc.get("length", "0") if enc is not None else "0",
            "enclosure_type": enc.get("type", "audio/mp4") if enc is not None else "audio/mp4",
            "episode": ep_num,
            "transcript_url": transcript_url,
        })
```

- [ ] **Step 8.3: Add a helper that injects `<podcast:transcript>` into a feedgen-produced XML**

Insert this function in `podpub.py` right after `build_feed`:

```python
def _inject_podcast_transcripts(feed_bytes: bytes, items: list[dict]) -> bytes:
    """Given feedgen-generated RSS bytes and the list of items, inject the
    Podcasting 2.0 namespace and a <podcast:transcript> child on each item
    that has a non-empty transcript_url. Items are matched by guid.
    """
    ET.register_namespace("podcast", PODCAST_NS)
    root = ET.fromstring(feed_bytes)
    root.set("xmlns:podcast", PODCAST_NS)

    guid_to_url = {it["guid"]: it["transcript_url"] for it in items if it.get("transcript_url")}
    if not guid_to_url:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    channel = root.find("channel")
    for item in channel.findall("item"):
        guid = item.findtext("guid", "")
        url = guid_to_url.get(guid)
        if not url:
            continue
        trans = ET.SubElement(item, f"{{{PODCAST_NS}}}transcript")
        trans.set("url", url)
        trans.set("type", "text/vtt")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
```

- [ ] **Step 8.4: Wire `build_feed` to call the injector**

Replace the existing `build_feed` function's final `return fg.rss_str(pretty=True)` with:

```python
    feed_bytes = fg.rss_str(pretty=True)
    return _inject_podcast_transcripts(feed_bytes, items)
```

- [ ] **Step 8.5: Verify feed parsing round-trips cleanly on the current feed**

```bash
.venv/bin/python -c "
from podpub import parse_existing_feed, build_feed
from pathlib import Path
import yaml
cfg = yaml.safe_load(open('config.yaml'))
_, items = parse_existing_feed(Path('feed.xml'))
print('parsed', len(items), 'items; transcripts:',
      sum(1 for i in items if i.get('transcript_url')))
out = build_feed(cfg, items)
print('rebuilt feed length:', len(out))
assert b'xmlns:podcast' in out, 'namespace missing'
print('namespace present:', b'xmlns:podcast' in out)
print('transcript tags:', out.count(b'<podcast:transcript'))
"
```

Expected:
- `parsed 7 items; transcripts: 0` (we haven't transcribed anything yet)
- `rebuilt feed length: ~20000` (similar to current)
- `namespace present: True`
- `transcript tags: 0`

The namespace is now in the feed even with zero transcripts — harmless, and future-proof.

- [ ] **Step 8.6: Run `--rebuild-feed --dry-run` to see the new feed shape**

```bash
.venv/bin/python podpub.py --rebuild-feed --dry-run 2>&1 | head -30
```

Expected: the feed preview includes `xmlns:podcast="https://podcastindex.org/namespace/1.0"` on the `<rss>` line. No `<podcast:transcript>` items yet (none have URLs).

- [ ] **Step 8.7: Commit**

```bash
git add podpub.py
git commit -m "Extend feed parsing/building for Podcasting 2.0 transcript tags"
```

---

## Task 9: `podpub.py` — `--backfill-transcripts` flag

**Files:**
- Modify: `podpub.py`

- [ ] **Step 9.1: Add the CLI flag**

In `main()` of `podpub.py`, find the `ap.add_argument` block and add after the existing `--rebuild-feed` argument:

```python
    ap.add_argument("--backfill-transcripts", action="store_true",
                    help="Generate VTTs for existing episodes missing them, add to feed, commit, push.")
    ap.add_argument("--skip-transcripts", action="store_true",
                    help="Publish without generating transcripts for new episodes.")
```

- [ ] **Step 9.2: Route `--backfill-transcripts` to a new handler**

In `main()`, find the `if args.rebuild_feed:` block. Add immediately after it:

```python
    if args.backfill_transcripts:
        return _backfill_transcripts(cfg, repo_dir, audio_dir, feed_path, base_url,
                                     audio_subdir, args, log)
```

- [ ] **Step 9.3: Implement `_backfill_transcripts`**

Insert this function in `podpub.py` after `_rebuild_feed`:

```python
def _backfill_transcripts(cfg: dict, repo_dir: Path, audio_dir: Path, feed_path: Path,
                          base_url: str, audio_subdir: str, args: argparse.Namespace,
                          log: logging.Logger) -> int:
    """Generate VTTs for existing audio/ entries that lack a sibling transcripts/ VTT.
    Rebuild feed with transcript URLs. Commit + push.
    """
    import transcribe  # lazy import so --help is instant and transcribe-only CLIs work

    transcripts_dir = repo_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    _, existing_items = parse_existing_feed(feed_path)
    if not existing_items:
        log.info("No existing items in feed; nothing to backfill.")
        return 0

    # Find audio files that have no VTT yet
    missing: list[tuple[Path, Path]] = []  # (audio_path, vtt_path)
    for audio_path in sorted(audio_dir.iterdir()):
        if not audio_path.is_file():
            continue
        if audio_path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        vtt_path = transcripts_dir / f"{audio_path.stem}.vtt"
        if not vtt_path.exists():
            missing.append((audio_path, vtt_path))

    if not missing:
        log.info("All existing episodes already have transcripts. Nothing to backfill.")
        return 0

    log.info("=== Backfill plan: %d episode(s) ===", len(missing))
    for a, v in missing:
        log.info("  %s -> %s", a.name, v.name)

    if args.dry_run:
        log.info("--dry-run: not transcribing.")
        return 0

    # Transcribe each; continue on per-episode failure
    failures: list[tuple[str, str]] = []
    new_vtts: list[Path] = []
    for audio_path, vtt_path in missing:
        try:
            log.info("transcribing: %s", audio_path.name)
            transcribe.transcribe_audio(audio_path, vtt_path, cfg)
            new_vtts.append(vtt_path)
        except Exception as e:  # intentional broad catch — per-episode isolation
            log.error("transcription FAILED for %s: %s", audio_path.name, e)
            failures.append((audio_path.name, str(e)))

    if not new_vtts:
        log.error("no episodes successfully transcribed; bailing without feed update.")
        return 1

    # Build transcript URL map keyed by guid, merge into existing_items
    guid_to_transcript_url = {}
    for it in existing_items:
        # Recover the audio filename from the enclosure URL to pair with the VTT
        name = f"{it['episode']:03d} - {it['title']}"
        vtt_name = f"{name}.vtt"
        vtt_path = transcripts_dir / vtt_name
        if vtt_path.exists():
            it["transcript_url"] = f"{base_url}/transcripts/{quote(vtt_name)}"
            guid_to_transcript_url[it["guid"]] = it["transcript_url"]

    log.info("transcript URLs in feed: %d", len(guid_to_transcript_url))

    # Rebuild feed
    feed_bytes = build_feed(cfg, existing_items)
    feed_path.write_bytes(feed_bytes)
    log.info("wrote feed: %s", feed_path)

    # Commit + push
    for v in new_vtts:
        git_run(repo_dir, "add", str(v.relative_to(repo_dir)), log=log)
    git_run(repo_dir, "add", str(feed_path.relative_to(repo_dir)), log=log)

    if len(new_vtts) == len(missing):
        commit_msg = f"Add transcripts for {len(new_vtts)} existing episode(s)"
    else:
        commit_msg = f"Add transcripts for {len(new_vtts)}/{len(missing)} existing episode(s)"
    git_run(repo_dir, "commit", "-m", commit_msg, log=log)
    log.info("committed: %s", commit_msg)

    if args.no_push:
        log.info("--no-push: skipping git push")
    else:
        git_run(repo_dir, "push", "-u", "origin", "main", log=log)
        log.info("pushed to origin main")

    if failures:
        log.error("=== Backfill completed with %d failure(s) ===", len(failures))
        for name, err in failures:
            log.error("  %s: %s", name, err)
        return 1
    return 0
```

- [ ] **Step 9.4: Dry-run the backfill**

```bash
.venv/bin/python podpub.py --backfill-transcripts --dry-run
```

Expected:
- `=== Backfill plan: 7 episode(s) ===`
- Each of the 7 existing audio files listed with its target VTT name.
- `--dry-run: not transcribing.`

- [ ] **Step 9.5: Commit (no backfill executed yet — that's Task 12)**

```bash
git add podpub.py
git commit -m "Add --backfill-transcripts flag to podpub.py"
```

---

## Task 10: `podpub.py` — integrate transcription into the publish flow

**Files:**
- Modify: `podpub.py`

- [ ] **Step 10.1: Add a VTT plan field and transcription step to publish flow**

In `podpub.py` `main()`, find the plan-building loop (`for i, src in enumerate(new_files, start=1):`). After the `stat = src.stat()` line and before `plans.append({...})`, nothing changes in the loop body. But we add a transcription step after the plan is built and before the move.

Find the block that starts with `log.info("=== Plan ===")` and ends with the dry-run check. After the dry-run block, and **before** the `moved_files: list[Path] = []` line, insert this transcription step:

```python
    # Transcribe each planned audio file. VTT lands in inbox alongside audio,
    # then we move it with the rest in the move loop below.
    if not args.skip_transcripts:
        import transcribe
        single_item = len(plans) == 1
        transcription_failures: list[tuple[str, str]] = []
        for p in plans:
            vtt_src = p["src"].with_suffix(".vtt")
            p["vtt_src"] = vtt_src
            p["vtt_dest"] = (repo_dir / "transcripts" / f"{p['dest'].stem}.vtt")
            try:
                log.info("transcribing: %s", p["src"].name)
                transcribe.transcribe_audio(p["src"], vtt_src, cfg, force=True)
            except Exception as e:
                log.error("transcription FAILED for %s: %s", p["src"].name, e)
                transcription_failures.append((p["src"].name, str(e)))
                if single_item:
                    log.error("single-item publish; aborting. Inbox untouched.")
                    return 1
                p["vtt_src"] = None  # mark as no-transcript; continue with others

        if transcription_failures and not single_item:
            log.warning("continuing publish despite %d transcription failure(s); "
                        "failed items ship without transcripts",
                        len(transcription_failures))
    else:
        log.info("--skip-transcripts: not generating transcripts")
        for p in plans:
            p["vtt_src"] = None
            p["vtt_dest"] = None
```

- [ ] **Step 10.2: Move VTT files alongside audio+md in the move loop**

Find the move loop (`for p in plans: shutil.move(...)`). After the existing sidecar-move block (the `for ext in (".md", ".txt"):` loop), add:

```python
        vtt_src = p.get("vtt_src")
        vtt_dest = p.get("vtt_dest")
        if vtt_src and vtt_src.exists() and vtt_dest:
            vtt_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(vtt_src), str(vtt_dest))
            moved_files.append(vtt_dest)
            log.info("moved transcript: %s", vtt_dest)
```

- [ ] **Step 10.3: Populate `transcript_url` on new items**

Find the `new_items = [_plan_to_item(p) for p in plans]` line. Replace `_plan_to_item` in podpub.py with an updated version that includes transcript_url:

Replace the existing `_plan_to_item` function with:

```python
def _plan_to_item(p: dict) -> dict:
    base_url = p.get("base_url", "")
    audio_subdir = p.get("audio_subdir", "audio")
    transcript_url = ""
    vtt_dest = p.get("vtt_dest")
    if vtt_dest is not None and vtt_dest.exists():
        transcript_url = f"{base_url}/transcripts/{quote(vtt_dest.name)}"
    return {
        "title": p["title"],
        "guid": p["guid"],
        "pub_date": p["pub_date"],
        "description": p["description"],
        "enclosure_url": p["url"],
        "enclosure_length": p["size"],
        "enclosure_type": p["mime"],
        "episode": p["episode"],
        "transcript_url": transcript_url,
    }
```

- [ ] **Step 10.4: Pass `base_url` and `audio_subdir` into each plan dict**

Find the `plans.append({...})` block inside the plan-building loop. Add these two fields (they're already in the surrounding scope as `base_url` and `audio_subdir`):

```python
        plans.append({
            "src": src,
            "dest": dest,
            "new_name": new_name,
            "episode": ep_num,
            "title": title,
            "description": description,
            "pub_date": formatdate(stat.st_mtime, localtime=False, usegmt=True),
            "size": stat.st_size,
            "mime": SUPPORTED_EXTS[src.suffix.lower()],
            "url": f"{base_url}/{quote(audio_subdir)}/{quote(new_name)}",
            "guid": make_guid(new_name),
            "has_sidecar": sidecar_text is not None,
            "base_url": base_url,
            "audio_subdir": audio_subdir,
        })
```

- [ ] **Step 10.5: Sanity-check the publish flow with a dry-run against an empty inbox**

```bash
.venv/bin/python podpub.py --dry-run
```

Expected: `No new audio files in inbox. Nothing to do.` (no transcription attempted).

- [ ] **Step 10.6: Commit**

```bash
git add podpub.py
git commit -m "Integrate transcription into publish flow; add --skip-transcripts"
```

---

## Task 11: Verify end-to-end on a test inbox file

**Files:** None (uses a temporary copy).

- [ ] **Step 11.1: Copy one existing audio file into the inbox (rename so it's not matched as already-processed)**

```bash
cp "audio/001 - Why AI Has A Body Problem.m4a" "inbox/Test_Publish_Flow.m4a"
cp "audio/001 - Why AI Has A Body Problem.md" "inbox/Test_Publish_Flow.md"
```

- [ ] **Step 11.2: Run a dry-run of publish**

```bash
.venv/bin/python podpub.py --dry-run
```

Expected:
- Plan lists 1 file: `Test_Publish_Flow.m4a -> 008 - Test Publish Flow.m4a` (episode number will be one past the current max).
- `--dry-run: no files moved, feed not written, no commit.`
- No transcription runs (dry-run skips it).

- [ ] **Step 11.3: Run publish with `--skip-transcripts` and `--no-push`**

```bash
.venv/bin/python podpub.py --skip-transcripts --no-push
```

Expected: file moved to `audio/`, commit created, no transcript generated.

- [ ] **Step 11.4: Roll back the test run**

```bash
git reset --hard HEAD~1
# The file was moved into audio/ then committed, so the reset undid the commit but left the file.
# Clean up:
ls "audio/008"* 2>/dev/null && rm "audio/008"*.m4a "audio/008"*.md 2>/dev/null
ls "inbox/Test_Publish_Flow"* 2>/dev/null && rm "inbox/Test_Publish_Flow"* 2>/dev/null
git status  # should be clean
```

Expected: `git status` shows a clean tree. No stray test files.

- [ ] **Step 11.5: Repeat with transcription this time — drop a fresh copy and run full publish**

```bash
cp "audio/001 - Why AI Has A Body Problem.m4a" "inbox/Test_Transcribe_Flow.m4a"
cp "audio/001 - Why AI Has A Body Problem.md" "inbox/Test_Transcribe_Flow.md"
.venv/bin/python podpub.py --no-push
```

Expected:
- Log shows `transcribing: Test_Transcribe_Flow.m4a`.
- After ~90 seconds, log shows `moved: audio/008 - Test Transcribe Flow.m4a` + `moved transcript: transcripts/008 - Test Transcribe Flow.vtt`.
- Feed written, commit created locally.

- [ ] **Step 11.6: Inspect the generated VTT and the feed entry**

```bash
head -20 "transcripts/008 - Test Transcribe Flow.vtt"
grep -A 2 "008 - Test Transcribe Flow" feed.xml | head -10
```

Expected:
- VTT starts with `WEBVTT` and has `<v Daniel>` / `<v Maya>` cues.
- Feed contains `<podcast:transcript url="https://synthyclaw.github.io/podpub/transcripts/008%20-%20Test%20Transcribe%20Flow.vtt" type="text/vtt"/>`.

- [ ] **Step 11.7: Roll back the test run completely**

```bash
git reset --hard HEAD~1
rm -f "audio/008"*.m4a "audio/008"*.md "transcripts/008"*.vtt
git status  # should be clean
```

Expected: `git status` clean, no stray files anywhere.

- [ ] **Step 11.8: (No commit — this task verifies, does not modify tracked files.)**

---

## Task 12: Execute the backfill for all 7 existing episodes

**Files:** Adds 7 files to `transcripts/` and modifies `feed.xml`.

- [ ] **Step 12.1: Dry-run the backfill**

```bash
.venv/bin/python podpub.py --backfill-transcripts --dry-run
```

Expected: lists all 7 episodes as missing transcripts.

- [ ] **Step 12.2: Execute the backfill, but don't push yet (`--no-push`)**

```bash
.venv/bin/python podpub.py --backfill-transcripts --no-push
```

Expected:
- For each of 7 episodes: `transcribing: NNN - Title.m4a` → `<path>: <K> cues, Daniel (X%) + Maya (Y%), duration Zs`.
- Total runtime: ~10–15 minutes on M1 Max (WhisperX + pyannote are cached after first episode).
- `wrote feed: feed.xml`.
- Commit created locally: `Add transcripts for 7 existing episode(s)`.

- [ ] **Step 12.3: Verify all 7 VTTs were produced**

```bash
ls -la transcripts/*.vtt | wc -l
```

Expected: `7`.

```bash
for f in transcripts/*.vtt; do
  head -1 "$f" | grep -q WEBVTT && echo "OK: $f" || echo "BAD: $f"
done
```

Expected: 7 `OK:` lines.

- [ ] **Step 12.4: Validate one VTT in detail — episode 001**

```bash
head -20 "transcripts/001 - Why AI Has A Body Problem.vtt"
```

Expected:
- `WEBVTT` first line.
- Cue blocks with `<v Daniel>...</v>` and `<v Maya>...</v>`.
- Timestamps in `HH:MM:SS.mmm --> HH:MM:SS.mmm` format.

**Required ear-check:** open `audio/001 - Why AI Has A Body Problem.m4a`. Jump to the timestamp of 2–3 random cues in the VTT (not just the first). Confirm speaker labels match voices. If they don't match, investigate before pushing.

- [ ] **Step 12.5: Verify the feed was updated correctly**

```bash
grep -c "<podcast:transcript" feed.xml
```

Expected: `7`.

```bash
grep "xmlns:podcast" feed.xml
```

Expected: one line containing `xmlns:podcast="https://podcastindex.org/namespace/1.0"`.

- [ ] **Step 12.6: (Optional but recommended) Validate feed.xml via a feed validator**

Upload `feed.xml` to https://validator.w3.org/feed/ or `feedvalidator.org`. Expected: passes with no errors. Warnings about the `podcast:` namespace are acceptable (some validators don't know Podcasting 2.0).

- [ ] **Step 12.7: Push**

```bash
git push origin main
```

Expected: push succeeds; GitHub Pages rebuilds within ~30 seconds.

- [ ] **Step 12.8: End-to-end Apple Podcasts acceptance test**

On an iOS 17.4+ device (or macOS Sonoma+ Podcasts app):
1. Open the podcast (`Library → Shows → NotebookLM Deep Dives`).
2. Pull down to refresh (may take up to an hour otherwise).
3. Open episode 001. Look for the transcript icon (speech-bubble). Tap it.
4. Confirm:
   - Transcript scrolls in sync with audio as it plays.
   - Speaker labels show `Daniel` and `Maya` (not `SPEAKER_00` or blank).
   - Text is legible and roughly matches what's said.

If Apple Podcasts shows no transcript icon, troubleshoot in this order:
- `curl` the transcript URL from the feed — returns VTT content? If 404, a path / URL-encoding mismatch.
- Check the feed in Apple's Podcasts Connect validator (if available) or another Podcasting 2.0-aware client like PocketCasts to rule out a client-side issue.
- Verify VTT is well-formed (passes any VTT validator).

- [ ] **Step 12.9: (No additional commit — Step 12.2 already committed; 12.7 pushed.)**

---

## Self-review notes

**Spec coverage:**
- Whisper toolchain + large-v3 model → Tasks 1, 4 (install + load).
- Daniel/Maya via F0 → Task 6.
- Backfill + forward integration → Tasks 9, 10, 12.
- VTT-only in `transcripts/` → Tasks 1, 7, 10.
- Podcasting 2.0 namespace + `<podcast:transcript>` → Task 8.
- `--skip-transcripts`, `--backfill-transcripts` → Tasks 9, 10.
- Error handling: single-item abort vs. batch continue → Tasks 9, 10.
- HF token setup + docs → Tasks 2, 3.
- Verification: smoke-test on 001, feed validation, Apple Podcasts check → Tasks 7, 12.
- Built-in sanity checks: speaker ratio warning, refuses overwrite → Task 7.

**Placeholder scan:** None. Every step has concrete code or commands.

**Type/signature consistency:**
- `transcribe_audio` signature (Task 7) matches invocations in Tasks 9 (`transcribe.transcribe_audio(audio_path, vtt_path, cfg)`) and 10 (`transcribe.transcribe_audio(p["src"], vtt_src, cfg, force=True)`). ✓
- `TranscriptionResult` dataclass fields (`num_cues`, `daniel_ratio`, `maya_ratio`, `duration_sec`, `model`) consistent throughout. ✓
- `PODCAST_NS` constant defined once (Task 8.1), referenced in `_inject_podcast_transcripts` and `parse_existing_feed`. ✓
- `parse_existing_feed` items dict gains `transcript_url` field (Task 8.2); `_inject_podcast_transcripts` reads `item.get("transcript_url")` (Task 8.3); `_plan_to_item` emits `transcript_url` (Task 10.3); backfill populates the same key (Task 9.3). ✓
