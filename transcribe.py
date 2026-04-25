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
import re
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


def _diarize(audio: np.ndarray, result: dict, cfg: dict) -> dict:
    """Diarize the audio and assign a speaker label to each word.

    Returns the aligned result mutated to include `speaker` on each word and
    on each segment. Speaker labels are pyannote-native (SPEAKER_00 / SPEAKER_01).
    """
    import whisperx
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    t = cfg["transcription"]
    model_name = t.get("diarization_model", "pyannote/speaker-diarization-3.1")
    log.info("loading diarization pipeline: %s", model_name)
    pipeline = DiarizationPipeline(model_name=model_name, token=t["hf_token"], device="cpu")

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
                # Always insert a separator; whitespace is normalized at the end.
                # Older whisperx versions had leading-space-on-word; newer ones don't.
                current["text"] += " " + word_text
            else:
                cues.append(current)
                current = {"start": w["start"], "end": w["end"],
                           "speaker": spk_name, "text": word_text}
    if current is not None:
        cues.append(current)
    # Normalize: collapse any run of whitespace to a single space, trim ends.
    for c in cues:
        c["text"] = re.sub(r"\s+", " ", c["text"]).strip()
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
