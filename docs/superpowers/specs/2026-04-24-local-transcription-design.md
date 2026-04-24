# Local Transcription for podpub

**Status:** Draft — pending approval
**Date:** 2026-04-24
**Owner:** Mohammad Soltaniehha

## Goal

Add local speech-to-text and speaker diarization to `podpub` so that every episode
in the RSS feed ships with a machine-generated transcript that Apple Podcasts
(and any other Podcasting 2.0 client) can render inline during playback, with
the two NotebookLM hosts labeled as **Daniel** (male voice) and **Maya** (female
voice).

All compute is local: no third-party API calls, no cloud transcription service.

## Non-Goals

- Translation to other languages.
- Overlap / interruption markers in the transcript (rare for NotebookLM).
- A transcript-editing UI. The VTT file is canonical; manual corrections happen
  by editing the file and re-running `--rebuild-feed`.
- A pytest test suite or any automated quality scoring. Verification is manual.

## User-Visible Changes

**New folder:** `transcripts/` at repo root, committed to git, served by GitHub
Pages at `<base_url>/transcripts/`.

**Filename parity:** For each `audio/NNN - Title.m4a`, there is a matching
`transcripts/NNN - Title.vtt` (same episode number, same title, `.vtt` extension).

**New RSS field per episode:** `<podcast:transcript url="..." type="text/vtt"/>`.

**New CLI flags on `podpub.py`:**
- `--backfill-transcripts` — one-shot; generate VTTs for every episode in
  `audio/` that lacks a sibling VTT, update the feed with transcript URLs,
  commit as `Add transcripts for existing episodes`, push.
- `--skip-transcripts` — publish a new episode without running the transcriber
  (escape hatch).

**New standalone module:** `transcribe.py`, runnable as a CLI
(`python transcribe.py <audio.m4a> --output <out.vtt>`) or importable from
`podpub.py`.

## Decisions

1. **Whisper toolchain: WhisperX.** Wraps faster-whisper + wav2vec2 alignment
   + pyannote.audio diarization. Chosen over `mlx-whisper` (faster but no
   diarization) and manual `faster-whisper` + `pyannote` stitching (more code
   to own).
2. **Model: `large-v3`.** The largest and highest-quality Whisper model.
   M1 Max with 64 GB handles it comfortably.
3. **Speaker labels: Daniel (male), Maya (female).** Assigned via F0
   (fundamental frequency) classification of diarized clusters: for each
   cluster, median F0 is computed with `librosa.pyin`; below 165 Hz → Daniel,
   above → Maya. Consistent across episodes because the NotebookLM voices
   themselves are stable.
4. **Transcript format: VTT only.** The Apple-facing format with native
   `<v Name>...</v>` speaker-label syntax. No SRT, no JSON archive.
5. **Scope: backfill + forward.** All 7 existing episodes transcribed in a
   one-shot backfill; every new episode transcribed as part of normal publish.
6. **Integration: separate module, invoked from `podpub.py`.** `transcribe.py`
   has one public entrypoint, is independently callable, and is imported by
   the publisher. Keeps transcription decoupled and independently re-runnable.

## Architecture

### New Module: `transcribe.py`

Public API:

```python
def transcribe_audio(
    audio_path: Path,
    output_vtt_path: Path,
    config: dict,
    *,
    force: bool = False,
) -> TranscriptionResult
```

`TranscriptionResult` is a small dataclass/dict with:
- `num_cues: int`
- `daniel_ratio: float`  # fraction of speech time
- `maya_ratio: float`
- `duration_sec: float`
- `model: str`

Raises on failure (missing HF token, corrupt audio, etc.). Refuses to overwrite
an existing VTT unless `force=True`.

CLI:

```
python transcribe.py <audio_path> [--output <vtt_path>] [--force]
```

If `--output` is omitted, writes to a sibling `.vtt` of the audio file.

### New Config Section

Appended to `config.yaml`:

```yaml
transcription:
  model: large-v3
  language: en
  hf_token: hf_xxxxx           # HuggingFace token for pyannote license
  male_label: Daniel
  female_label: Maya
  f0_threshold_hz: 165         # below = male label, above = female
```

There is no `enabled` flag; use `--skip-transcripts` on the CLI to bypass
transcription for a single run.

`config.yaml.example` in `setup/` gains the same section with placeholder values
and a comment pointing at the two HuggingFace license URLs that must be
accepted.

### New Dependencies

Appended to `setup/requirements.txt`:

```
whisperx
torch
torchaudio
librosa
```

Exact version pins TBD during implementation — WhisperX is sensitive to torch
versions. The implementation plan picks pins known to work together on
Apple Silicon.

### Folder: `transcripts/`

- Committed to git.
- Contents served by GitHub Pages at `<base_url>/transcripts/`.
- A `.gitkeep` placeholder ensures the folder exists pre-first-transcription.
- `.gitignore` is unchanged — VTTs are tracked.

## Data Flow

### Per-episode transcription pipeline (inside `transcribe.py`)

1. **Load audio.** `ffmpeg` decodes to 16 kHz mono PCM (via librosa).
2. **Transcribe.** WhisperX → faster-whisper `large-v3` → segment list with
   text and coarse timestamps.
3. **Align.** WhisperX's wav2vec2 forced aligner → word-level timestamps.
4. **Diarize.** `pyannote.audio` speaker-diarization-3.1, forced to
   `num_speakers=2`. Returns speaker turns (`SPEAKER_00`, `SPEAKER_01`).
5. **Merge.** WhisperX combines word timestamps with diarization turns. Each
   word gets a speaker label.
6. **Classify speakers.** For each of the two clusters, concatenate the first
   ~3 seconds of cumulative speech from that cluster, compute median F0 via
   `librosa.pyin`, compare to `f0_threshold_hz`. Build
   `{SPEAKER_00: "Daniel", SPEAKER_01: "Maya"}` mapping (or the reverse).
7. **Collapse to cues.** Group consecutive words from the same speaker into a
   single cue. Break on speaker change or when cue duration exceeds ~15 s.
8. **Emit VTT.** Write `<v Daniel>...</v>` / `<v Maya>...</v>` cues with
   `HH:MM:SS.mmm` timestamps to the output file.

### Publish integration (inside `podpub.py`)

Today's flow:

```
scan_inbox → plan → move audio+md → build feed → commit → push
```

New flow:

```
scan_inbox → plan
           → TRANSCRIBE each new file (VTT written to inbox alongside audio)
           → move audio+md+vtt into audio/ and transcripts/
           → build feed (with transcript URLs)
           → commit → push
```

Transcription happens **before** any `mv`, so a failure aborts cleanly with the
inbox intact. `--skip-transcripts` bypasses the transcribe step entirely
(episode ships with no `<podcast:transcript>` tag; can be filled later with
`--backfill-transcripts`). `--dry-run` logs what would be transcribed but does
not run WhisperX.

### Backfill flow (`--backfill-transcripts`)

1. Walk `audio/*.{m4a,mp3,wav}`.
2. For each, check whether a sibling `transcripts/<same stem>.vtt` exists;
   collect the ones that don't.
3. Transcribe each missing one.
4. Rebuild `feed.xml` with transcript URLs for all episodes now present.
5. Commit all new VTTs + updated feed in one commit:
   `Add transcripts for existing episodes`.
6. Push (unless `--no-push`).

Idempotent: re-running skips episodes that already have VTTs. Manual
re-transcription recipe: delete the VTT, re-run backfill.

### Feed XML changes

- Root `<rss>` element gains `xmlns:podcast="https://podcastindex.org/namespace/1.0"`.
- Each `<item>` gains `<podcast:transcript url="..." type="text/vtt"/>` when a
  transcript exists for that episode.
- `feedgen` has no Podcasting 2.0 extension, so we post-process the emitted
  XML with `xml.etree.ElementTree` to inject the namespace declaration and
  per-item transcript elements. Keeps the feedgen-based build loop intact.
- `parse_existing_feed` is extended to read existing `<podcast:transcript>`
  URLs so that `--rebuild-feed` preserves them.

## Error Handling

**Transcription failures**
- Missing HF token → fail fast with a message pointing at `config.yaml` and
  the two license URLs. No partial work.
- **Single-item publish** (one file in inbox) fails during transcription →
  abort the whole publish. No audio moved, no feed updated, inbox intact.
  User can fix the problem and re-run, or add `--skip-transcripts`.
- **Batch run** (multiple files in inbox, or `--backfill-transcripts`) —
  one item fails mid-batch → log the error, skip that item, continue with the
  rest, exit non-zero at the end with a per-episode summary of failures. The
  successful items still ship.
- Corrupt/silent/too-short audio → catch, log, skip, no VTT written. Handled
  as a per-item failure per the rules above.

**Diarization edge cases**
- Only one speaker cluster detected → emit VTT without `<v>` labels; log a
  warning. (Unusual for NotebookLM but possible on very short clips.)
- F0 classification ambiguous (both clusters near threshold) → use relative
  pitch: lower-F0 cluster → Daniel, higher → Maya. Always labels; never blocks.
- Three or more clusters despite `num_speakers=2` → keep the two dominant
  clusters by total speaking time, merge stragglers into the nearest by
  embedding distance.

**Publish-path failures**
- `--skip-transcripts` on a new episode → audio ships, no transcript tag in
  feed. Fixable later via `--backfill-transcripts`.
- Transcription succeeds, publish fails before commit → VTT is staged in inbox
  alongside audio; normal rerun picks it up without retranscribing.

**Feed XML consistency**
- `--rebuild-feed` preserves existing transcript URLs read from the prior feed.
- If a transcript URL is in the feed but its VTT is missing from `transcripts/`,
  log a warning during rebuild; don't auto-heal.

## Verification Plan

Manual, not automated. Intended as a rollout checklist:

1. **Smoke test on one episode** before the 7-episode batch. Run
   `transcribe.py` standalone on episode 001, inspect the VTT:
   - Daniel/Maya labels match the voices (listen + compare at 2–3 timestamps).
   - Speaker turns line up with audio (spot-check 3 cues).
   - Technical terms like "V-JEPA" are transcribed correctly.
2. **RSS feed validation.** After the first transcript lands in `feed.xml`,
   run it through `validator.w3.org/feed/` to confirm the `podcast:` namespace
   and `<podcast:transcript>` tag are valid.
3. **Apple Podcasts end-to-end.** Push one transcribed episode. On iOS 17.4+,
   open in Apple Podcasts, tap the transcript icon, confirm speaker labels and
   timing render correctly. This is the real acceptance test.
4. **Dry-run preview.** `--backfill-transcripts --dry-run` must list exactly
   which episodes will be transcribed without calling WhisperX.
5. **Idempotency check.** After full backfill, `--rebuild-feed` is a no-op
   (no diff); `--backfill-transcripts` re-run finds nothing to do.

**Built-in sanity checks:**
- Post-transcription, `transcribe.py` reads the VTT back and logs
  `NNN.vtt: <K> cues, Daniel (<X>%) + Maya (<Y>%)`.
- If either speaker is <20% or >80% of total speech time, log a diarization
  warning (unlikely but worth surfacing).
- `transcribe.py` refuses to overwrite an existing VTT without `--force`.

## Setup Impact

One-time user setup additions (added to `CLAUDE.md` under a new
"Transcription setup" section):

1. Create a HuggingFace account (free).
2. Accept the license on two pyannote models:
   - `pyannote/speaker-diarization-3.1`
   - `pyannote/segmentation-3.0`
3. Generate an HF access token (read scope), paste into `config.yaml`.
4. Re-install requirements: `pip install -r setup/requirements.txt`.
5. First transcription downloads ~3 GB of model weights to
   `~/.cache/huggingface/` — happens once.

## Open Implementation Questions

Deferred to the implementation plan, not blocking the spec:

- Exact version pins for `whisperx`, `torch`, `torchaudio` on Apple Silicon.
- Whether to run faster-whisper on CPU or MPS (benchmark both during
  implementation).
- Batch size / VAD settings for WhisperX — use defaults unless they cause
  issues.
- Whether to cache WhisperX model objects across episodes during a single
  backfill run (should save 30–60 s per episode after the first).
