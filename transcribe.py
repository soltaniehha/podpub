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
