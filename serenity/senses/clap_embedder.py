"""CLAP audio embedding — laion/clap-htsat-unfused via transformers.

Install:  pip install transformers torch soundfile

Produces 512-dim embeddings suitable for cosine similarity search in AudioRAG.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

_model = None
_processor = None
_MODEL_ID = "laion/clap-htsat-unfused"


def _load():
    global _model, _processor
    if _model is not None:
        return _model, _processor
    try:
        from transformers import ClapModel, ClapProcessor  # type: ignore
        logger.info("Loading CLAP model '{}'…", _MODEL_ID)
        _processor = ClapProcessor.from_pretrained(_MODEL_ID)
        _model = ClapModel.from_pretrained(_MODEL_ID)
        _model.eval()
        logger.info("CLAP model loaded.")
        return _model, _processor
    except ImportError as exc:
        raise RuntimeError(
            "CLAP requires the 'transformers' and 'soundfile' packages.\n"
            "Run:  pip install transformers soundfile"
        ) from exc


def embed_audio(audio_path: str | Path) -> list[float]:
    """Embed an audio file into a 512-dim CLAP vector."""
    import torch
    import soundfile as sf  # type: ignore

    model, processor = _load()
    audio, sr = sf.read(str(audio_path))
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # stereo → mono

    inputs = processor(audios=audio, sampling_rate=sr, return_tensors="pt")
    with torch.no_grad():
        features = model.get_audio_features(**inputs)
        # L2 normalise
        features = features / features.norm(dim=-1, keepdim=True)
    return features[0].tolist()


def embed_text(text: str) -> list[float]:
    """Embed a text query into a 512-dim CLAP vector for cross-modal retrieval."""
    import torch

    model, processor = _load()
    inputs = processor(text=[text], return_tensors="pt", padding=True)
    with torch.no_grad():
        features = model.get_text_features(**inputs)
        features = features / features.norm(dim=-1, keepdim=True)
    return features[0].tolist()
