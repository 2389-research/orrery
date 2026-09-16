# ABOUTME: Image embedding via SigLIP (local) or Vertex AI multimodal (cloud).
# ABOUTME: Produces embeddings in a shared vision-language space for cross-modal search.

from __future__ import annotations
from pathlib import Path
import numpy as np

# Lazy-loaded model cache — avoids reloading ~400MB model per call
_siglip_model = None
_siglip_processor = None


def _get_siglip():
    """Lazy-load SigLIP model and processor."""
    global _siglip_model, _siglip_processor
    if _siglip_model is None:
        from transformers import AutoProcessor, AutoModel
        _siglip_model = AutoModel.from_pretrained("google/siglip-base-patch16-224")
        _siglip_processor = AutoProcessor.from_pretrained("google/siglip-base-patch16-224")
    return _siglip_model, _siglip_processor


def embed_image(path: Path) -> np.ndarray | None:
    """Embed an image using SigLIP's projection head. Returns normalized embedding
    in the shared text/image latent space, or None if unavailable.

    NOTE: must use `get_image_features` (with the projection) — `model.vision_model(...)`
    returns raw transformer features that are NOT in the same space as text features.
    Mixing them yields meaningless similarity scores.
    """
    try:
        import torch
        from PIL import Image

        model, processor = _get_siglip()
        image = Image.open(path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        vision_inputs = {k: v for k, v in inputs.items() if k not in ("input_ids", "attention_mask")}
        with torch.no_grad():
            out = model.get_image_features(**vision_inputs)
        # transformers 5.x returns BaseModelOutputWithPooling — use the pooled projection
        features = out.pooler_output if hasattr(out, "pooler_output") else out
        embedding = features[0].numpy()
        return embedding / (np.linalg.norm(embedding) + 1e-8)
    except Exception as e:
        print(f"[image_embedding] {type(e).__name__}: {e}", flush=True)
        return None


def embed_image_text(text: str) -> np.ndarray | None:
    """Embed text using SigLIP's text projection head — same latent space as image embeddings.

    Used for: embedding image descriptions, entity names, and text queries
    into the image index for cross-modal retrieval.
    """
    try:
        import torch

        model, processor = _get_siglip()
        # SigLIP requires padding to max_length — padding=True only pads to batch
        # max, leaving short queries with too-few tokens for the pooled output to
        # land in the same space as image embeddings.
        inputs = processor(text=[text], return_tensors="pt", padding="max_length", truncation=True)
        text_inputs = {k: v for k, v in inputs.items() if k != "pixel_values"}
        with torch.no_grad():
            out = model.get_text_features(**text_inputs)
        features = out.pooler_output if hasattr(out, "pooler_output") else out
        embedding = features[0].numpy()
        return embedding / (np.linalg.norm(embedding) + 1e-8)
    except Exception as e:
        print(f"[image_embedding] {type(e).__name__}: {e}", flush=True)
        return None


def embed_image_batch(paths: list[Path]) -> list[np.ndarray | None]:
    """Embed multiple images in a batch."""
    try:
        import torch
        from PIL import Image

        model, processor = _get_siglip()
        images = [Image.open(p).convert("RGB") for p in paths]
        inputs = processor(images=images, return_tensors="pt")
        with torch.no_grad():
            outputs = model.get_image_features(**inputs)
        embeddings = outputs.numpy()
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return list(embeddings / norms)
    except ImportError:
        return [None] * len(paths)


def embed_text_batch(texts: list[str]) -> list[np.ndarray | None]:
    """Embed multiple texts via SigLIP text encoder in a batch."""
    try:
        import torch

        model, processor = _get_siglip()
        inputs = processor(text=texts, return_tensors="pt", padding="max_length", truncation=True)
        with torch.no_grad():
            out = model.get_text_features(**inputs)
        features = out.pooler_output if hasattr(out, "pooler_output") else out
        embeddings = features.numpy()
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return list(embeddings / norms)
    except ImportError:
        return [None] * len(texts)
