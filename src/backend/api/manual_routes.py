"""Manual run route: POST /api/sessions/<session_id>/run-code.

A session no longer has a locked "mode" (see DECISIONS D-018) — any session
can run manual code, independent of whether it's also had LLM chat turns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify

from ..config import config
from ..core.auth.middleware import login_required, get_current_user_id
from ..core.sandbox.backend import get_executor
from ..core.image.thumbnail import generate_thumbnail
from ..core.session.manager import get_manager
from ..models.dto import GeneratedImage
from ..utils.slugify import slugify
from .upload_routes import get_image_registry

manual_bp = Blueprint("manual", __name__)


@manual_bp.route("/sessions/<session_id>/run-code", methods=["POST"])
@login_required
def run_code(session_id: str):
    """Execute user-submitted code in the sandbox. Owner only.

    Single execution — no LLM, no retry loop.
    """
    manager = get_manager()
    sess = manager.get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    if sess.status == "running":
        return jsonify({"error": "Session is already running"}), 409

    data = request.get_json(force=True)
    code = data.get("code", "")
    if not code.strip():
        return jsonify({"error": "No code provided"}), 400

    # Persist code on the session (survives switching to chat mode and back)
    manager.save_manual_code(session_id, code)
    if not manager.try_start_run(session_id):
        return jsonify({"error": "Session is already running"}), 409
    try:
        run_num = sess.next_run_number()

        image_registry = get_image_registry()

        first_result = None
        all_success = True

        for slot_idx, img_id in enumerate(sess.image_ids):
            metadata = image_registry.get(img_id)
            if not metadata:
                continue

            input_path = metadata.path

            # Unique dir per run + per slot — user code often saves a fixed filename,
            # so a shared dir makes slots/runs overwrite each other's outputs.
            slot_output_dir = config.OUTPUTS_DIR / session_id / f"run{run_num}" / f"slot{slot_idx}"
            slot_output_dir.mkdir(parents=True, exist_ok=True)

            # Execute
            result = get_executor().execute(
                code=code,
                input_path=input_path,
                output_dir=str(slot_output_dir),
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
                    session_id=session_id,
                    description=description,
                    path=str(out_path),
                    preview_path=str(preview_path) if preview_path.exists() else "",
                    code=code,
                    source_turn_index=None,
                    produced_at=datetime.utcnow(),
                    source_iteration=run_num,
                )
                manager.add_output(session_id, gen_image)
            else:
                all_success = False
    finally:
        manager.set_status(session_id, "idle")

    if not first_result:
        return jsonify({"error": "No images processed"}), 400

    result_dict = first_result.to_dict()
    # Signal to the frontend that this was a batch run and it should refetch outputs
    result_dict["batch_run"] = True
    result_dict["all_success"] = all_success

    return jsonify(result_dict), 200
