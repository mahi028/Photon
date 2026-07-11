"""Manual window run route: POST /api/windows/<window_id>/run-code."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify

from ..config import config
from ..core.sandbox.subprocess_executor import SubprocessExecutor
from ..core.image.thumbnail import generate_thumbnail
from ..core.session.manager import get_manager
from ..models.dto import GeneratedImage
from ..utils.slugify import slugify
from .upload_routes import get_image_registry

manual_bp = Blueprint("manual", __name__)
_executor = SubprocessExecutor()


@manual_bp.route("/windows/<window_id>/run-code", methods=["POST"])
def run_code(window_id: str):
    """Execute user-submitted code in the sandbox (manual mode only).

    Single execution — no LLM, no retry loop.
    """
    manager = get_manager()
    window = manager.get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404
    if window.mode not in ("manual", "llm"):
        return jsonify({"error": "Invalid window mode"}), 400
    if window.status == "running":
        return jsonify({"error": "Window is already running"}), 409

    data = request.get_json(force=True)
    code = data.get("code", "")
    if not code.strip():
        return jsonify({"error": "No code provided"}), 400

    # Persist code in window (only applies to manual mode)
    manager.save_manual_code(window_id, code)
    manager.set_status(window_id, "running")

    run_num = window.next_run_number()
    output_dir = config.OUTPUTS_DIR / window_id
    output_dir.mkdir(parents=True, exist_ok=True)
    
    image_registry = get_image_registry()
    
    first_result = None
    all_success = True

    for slot_idx, img_id in enumerate(window.image_ids):
        metadata = image_registry.get(img_id)
        if not metadata:
            continue
            
        input_path = metadata.path
        
        # Execute
        result = _executor.execute(
            code=code,
            input_path=input_path,
            output_dir=str(output_dir),
            timeout=config.EXECUTION_TIMEOUT_SECONDS,
        )
        
        if slot_idx == 0:
            first_result = result
            
        if result.file_exists and result.output_path:
            # Generate preview for the output
            out_path = Path(result.output_path)
            preview_path = out_path.parent / f"{out_path.stem}_preview.jpg"
            try:
                generate_thumbnail(out_path, preview_path)
            except Exception:
                pass

            # Write sibling .py file for provenance
            code_path = out_path.with_suffix(".py")
            try:
                code_path.write_text(code, encoding="utf-8")
            except Exception:
                pass

            output_image_id = uuid.uuid4().hex
            description = f"manual_run_slot{slot_idx}_{run_num}"

            gen_image = GeneratedImage(
                image_id=output_image_id,
                window_id=window_id,
                description=description,
                path=str(out_path),
                preview_path=str(preview_path) if preview_path.exists() else "",
                code=code,
                source_turn_index=None,
                produced_at=datetime.utcnow(),
                source_iteration=run_num,
            )
            manager.add_output(window_id, gen_image)
        else:
            all_success = False

    manager.set_status(window_id, "idle")

    if not first_result:
        return jsonify({"error": "No images processed"}), 400

    result_dict = first_result.to_dict()
    # Signal to the frontend that this was a batch run and it should refetch outputs
    result_dict["batch_run"] = True
    result_dict["all_success"] = all_success

    return jsonify(result_dict), 200
