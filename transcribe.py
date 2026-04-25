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
