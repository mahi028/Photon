"""Model declarations — the ONLY sanctioned place to load model weights.

Each entry is a `loader(device) -> handle` registered with `@register_model`.
None of it runs at import: ModelManager calls the loader the first time an op
actually asks for the model, and unloads it once idle (see manager.py).

`guard.py` blocks these same loading calls anywhere under core/pipeline/ops/,
so this file stays the single place model lifetimes begin.
"""

from __future__ import annotations

import logging

from .registry import register_model

logger = logging.getLogger(__name__)


@register_model(
    name="dinov2_vits14",
    vram_mb=900,
    doc="DINOv2 ViT-S/14 — self-supervised patch features (21M params).",
)
def _load_dinov2_vits14(device):
    import torch
    model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14", verbose=False)
    return model.to(device).eval()


@register_model(
    name="dinov2_vitb14",
    vram_mb=1600,
    doc="DINOv2 ViT-B/14 — self-supervised patch features (86M params), richer than ViT-S.",
)
def _load_dinov2_vitb14(device):
    import torch
    model = torch.hub.load("facebookresearch/dinov2", "dinov2_vitb14", verbose=False)
    return model.to(device).eval()


@register_model(
    name="deeplabv3_resnet50",
    vram_mb=1200,
    doc="DeepLabV3+ResNet-50 semantic segmentation (COCO/VOC 21 classes) for "
        "model-based background removal.",
)
def _load_deeplabv3(device):
    from torchvision.models.segmentation import (
        DeepLabV3_ResNet50_Weights, deeplabv3_resnet50,
    )
    model = deeplabv3_resnet50(weights=DeepLabV3_ResNet50_Weights.DEFAULT)
    return model.to(device).eval()
