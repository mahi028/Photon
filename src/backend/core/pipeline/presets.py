"""Built-in preset pipelines, seeded into the DB (user_id NULL = global, read-only).

Presets are clone-to-edit: the UI copies one into the user's library rather
than editing in place.
"""

from __future__ import annotations

PRESETS: list[dict] = [
    {
        "slug": "stain-mask",
        "title": "Stain Mask (H&E)",
        "description": "Grayscale → optical density → threshold → closing → binary mask. "
                       "Classic stain-area segmentation for histology.",
        "graph": {
            "nodes": [
                {"id": "gray", "op": "to_grayscale", "params": {}},
                {"id": "od", "op": "optical_density", "params": {"background": 255.0}},
                {"id": "thr", "op": "threshold_float_map", "params": {"threshold": 0.15}},
                {"id": "close", "op": "morph_closing", "params": {"radius": 3}},
                {"id": "out", "op": "mask_to_image", "params": {}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["gray", "image"]},
                {"from": ["gray", "gray"], "to": ["od", "image"]},
                {"from": ["od", "od"], "to": ["thr", "map"]},
                {"from": ["thr", "mask"], "to": ["close", "mask"]},
                {"from": ["close", "mask"], "to": ["out", "mask"]},
            ],
        },
    },
    {
        "slug": "background-cleanup",
        "title": "Background Cleanup (classical)",
        "description": "Blur → Otsu → keep the largest component → apply mask. "
                       "Isolates the main subject without any model.",
        "graph": {
            "nodes": [
                {"id": "gray", "op": "to_grayscale", "params": {}},
                {"id": "blur", "op": "gaussian_blur", "params": {"sigma": 2.0}},
                {"id": "thr", "op": "otsu_threshold", "params": {"invert": True}},
                {"id": "big", "op": "largest_component", "params": {"count": 1}},
                {"id": "fill", "op": "fill_holes", "params": {}},
                {"id": "cut", "op": "apply_mask", "params": {"background": 255}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["gray", "image"]},
                {"from": ["gray", "gray"], "to": ["blur", "image"]},
                {"from": ["blur", "image"], "to": ["thr", "image"]},
                {"from": ["thr", "mask"], "to": ["big", "mask"]},
                {"from": ["big", "mask"], "to": ["fill", "mask"]},
                {"from": ["input", "image"], "to": ["cut", "image"]},
                {"from": ["fill", "mask"], "to": ["cut", "mask"]},
            ],
        },
    },
    {
        "slug": "edge-sketch",
        "title": "Edge Sketch",
        "description": "Grayscale → blur → Sobel → normalize → invert. "
                       "Pencil-sketch style edge rendering.",
        "graph": {
            "nodes": [
                {"id": "gray", "op": "to_grayscale", "params": {}},
                {"id": "blur", "op": "gaussian_blur", "params": {"sigma": 1.0}},
                {"id": "edges", "op": "sobel_edges", "params": {}},
                {"id": "norm", "op": "normalize_map", "params": {}},
                {"id": "inv", "op": "invert", "params": {}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["gray", "image"]},
                {"from": ["gray", "gray"], "to": ["blur", "image"]},
                {"from": ["blur", "image"], "to": ["edges", "image"]},
                {"from": ["edges", "edges"], "to": ["norm", "map"]},
                {"from": ["norm", "image"], "to": ["inv", "image"]},
            ],
        },
    },
    {
        "slug": "clahe-enhance",
        "title": "CLAHE Enhance",
        "description": "Grayscale → CLAHE → percentile contrast stretch. "
                       "Brings out local detail in low-contrast images.",
        "graph": {
            "nodes": [
                {"id": "gray", "op": "to_grayscale", "params": {}},
                {"id": "clahe", "op": "clahe", "params": {"clip_limit": 0.02}},
                {"id": "stretch", "op": "stretch_contrast",
                 "params": {"low_pct": 2.0, "high_pct": 98.0}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["gray", "image"]},
                {"from": ["gray", "gray"], "to": ["clahe", "image"]},
                {"from": ["clahe", "image"], "to": ["stretch", "image"]},
            ],
        },
    },
    {
        "slug": "nuclei-segmentation",
        "title": "Nuclei Segmentation (hematoxylin)",
        "description": "H&E deconvolution → threshold → opening → fill holes → "
                       "remove specks → overlay on the original.",
        "graph": {
            "nodes": [
                {"id": "hem", "op": "hed_deconvolution", "params": {"stain": "hematoxylin"}},
                {"id": "thr", "op": "threshold_float_map", "params": {"threshold": 0.02}},
                {"id": "open", "op": "morph_opening", "params": {"radius": 2}},
                {"id": "fill", "op": "fill_holes", "params": {}},
                {"id": "clean", "op": "remove_small_objects", "params": {"min_size": 40}},
                {"id": "overlay", "op": "mask_overlay",
                 "params": {"alpha": 0.45, "color": "red"}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["hem", "image"]},
                {"from": ["hem", "channel"], "to": ["thr", "map"]},
                {"from": ["thr", "mask"], "to": ["open", "mask"]},
                {"from": ["open", "mask"], "to": ["fill", "mask"]},
                {"from": ["fill", "mask"], "to": ["clean", "mask"]},
                {"from": ["input", "image"], "to": ["overlay", "image"]},
                {"from": ["clean", "mask"], "to": ["overlay", "mask"]},
            ],
        },
    },
    {
        "slug": "background-removal-model",
        "title": "Background Removal (model)",
        "description": "DeepLabV3 semantic segmentation → clean up the mask → cut out "
                       "the background. Model-based counterpart to the classical preset; "
                       "the model loads on demand and unloads after idle (D-030).",
        "graph": {
            "nodes": [
                {"id": "seg", "op": "segment_foreground",
                 "params": {"target_class": "any", "threshold": 0.5}},
                {"id": "fill", "op": "fill_holes", "params": {}},
                {"id": "clean", "op": "remove_small_objects", "params": {"min_size": 200}},
                {"id": "cut", "op": "apply_mask", "params": {"background": 255}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["seg", "image"]},
                {"from": ["seg", "mask"], "to": ["fill", "mask"]},
                {"from": ["fill", "mask"], "to": ["clean", "mask"]},
                {"from": ["input", "image"], "to": ["cut", "image"]},
                {"from": ["clean", "mask"], "to": ["cut", "mask"]},
            ],
        },
    },
    {
        "slug": "dinov2-features",
        "title": "DINOv2 Feature Map",
        "description": "DINOv2 ViT-S/14 self-supervised patch features → channel mean → "
                       "normalized viewable map. Model-based counterpart to MIND.",
        "graph": {
            "nodes": [
                {"id": "feat", "op": "dinov2_features", "params": {"resize_to": 518}},
                {"id": "mean", "op": "feature_mean", "params": {}},
                {"id": "norm", "op": "normalize_map", "params": {}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["feat", "image"]},
                {"from": ["feat", "features"], "to": ["mean", "features"]},
                {"from": ["mean", "map"], "to": ["norm", "map"]},
            ],
        },
    },
    {
        "slug": "mind-features",
        "title": "MIND Feature Map",
        "description": "Grayscale → MIND self-similarity descriptor → channel mean → "
                       "normalized viewable map. Contrast/modality invariant structure.",
        "graph": {
            "nodes": [
                {"id": "gray", "op": "to_grayscale", "params": {}},
                {"id": "mind", "op": "mind_descriptor",
                 "params": {"sigma": 0.5, "patch_radius": 1}},
                {"id": "mean", "op": "feature_mean", "params": {}},
                {"id": "norm", "op": "normalize_map", "params": {}},
            ],
            "edges": [
                {"from": ["input", "image"], "to": ["gray", "image"]},
                {"from": ["gray", "gray"], "to": ["mind", "image"]},
                {"from": ["mind", "features"], "to": ["mean", "features"]},
                {"from": ["mean", "map"], "to": ["norm", "map"]},
            ],
        },
    },
]
