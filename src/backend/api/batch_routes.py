"""Batch runs (D-046).

  POST   /api/batches                 {pipeline_id | published_id (+version) | graph,
                                        image_ids?, session_ids?, title?}  -> 201 batch
  POST   /api/batches/upload          multipart: files (images and/or .zip archives)
                                        -> {images: [{image_id, filename}], skipped: [...]}
  GET    /api/batches                 your batches
  GET    /api/batches/<id>            status, items, value columns, tables
  POST   /api/batches/<id>/cancel
  POST   /api/batches/<id>/retry      re-run failed / cancelled images
  DELETE /api/batches/<id>
  GET    /api/batches/<id>/values.csv
  GET    /api/batches/<id>/table.csv?key=<node.port>
  GET    /api/batches/<id>/files/<path>    an output image or preview
  GET    /api/batches/<id>/download.zip
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request, send_file

from ..core.auth.middleware import get_current_user_id, login_required
from ..core.pipeline import batch

batch_bp = Blueprint("batches", __name__)

def _err(e: batch.BatchError):
    body = {"error": str(e)}
    if e.issues:
        body["issues"] = e.issues
    return jsonify(body), e.status


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@batch_bp.route("/batches", methods=["POST"])
@login_required
def create_batch():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400
    uid = get_current_user_id()
    try:
        images = batch.images_for(uid, data.get("image_ids"), data.get("session_ids"))
        b = batch.create(uid, images, title=data.get("title"),
                         pipeline_id=data.get("pipeline_id"), published_id=data.get("published_id"),
                         version=_int(data.get("version")), graph=data.get("graph"))
    except batch.BatchError as e:
        return _err(e)
    return jsonify(b), 201


@batch_bp.route("/batches/upload", methods=["POST"])
@login_required
def upload_many():
    """Many images at once, loose or zipped. Each one becomes an image you own."""
    files = [(f.filename, f.stream) for f in request.files.getlist("files")]
    if not files:
        return jsonify({"error": "No files (multipart field 'files')."}), 400
    images, skipped = batch.ingest_files(get_current_user_id(), files)
    return jsonify({"images": images, "skipped": skipped}), 200


@batch_bp.route("/batches", methods=["GET"])
@login_required
def list_batches():
    from ..core.session import store
    return jsonify({"batches": store.list_batches(get_current_user_id())}), 200


@batch_bp.route("/batches/<batch_id>", methods=["GET"])
@login_required
def get_batch(batch_id: str):
    try:
        return jsonify(batch.get(get_current_user_id(), batch_id,
                                 with_items=request.args.get("items", "1") != "0")), 200
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>/cancel", methods=["POST"])
@login_required
def cancel_batch(batch_id: str):
    try:
        return jsonify(batch.cancel(get_current_user_id(), batch_id)), 200
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>/retry", methods=["POST"])
@login_required
def retry_batch(batch_id: str):
    try:
        return jsonify(batch.retry_failed(get_current_user_id(), batch_id)), 200
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>", methods=["DELETE"])
@login_required
def delete_batch(batch_id: str):
    try:
        batch.delete(get_current_user_id(), batch_id)
    except batch.BatchError as e:
        return _err(e)
    return "", 204


def _csv(text: str, name: str) -> Response:
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@batch_bp.route("/batches/<batch_id>/values.csv", methods=["GET"])
@login_required
def values_csv(batch_id: str):
    try:
        return _csv(batch.values_csv(get_current_user_id(), batch_id), f"batch_{batch_id[:8]}_values.csv")
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>/table.csv", methods=["GET"])
@login_required
def table_csv(batch_id: str):
    key = request.args.get("key") or ""
    try:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return _csv(batch.table_csv(get_current_user_id(), batch_id, key), f"batch_{batch_id[:8]}_{safe}.csv")
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>/files/<path:rel>", methods=["GET"])
@login_required
def batch_file(batch_id: str, rel: str):
    try:
        return send_file(batch.output_file(get_current_user_id(), batch_id, rel), max_age=3600)
    except batch.BatchError as e:
        return _err(e)


@batch_bp.route("/batches/<batch_id>/download.zip", methods=["GET"])
@login_required
def download_zip(batch_id: str):
    try:
        path = batch.zip_all(get_current_user_id(), batch_id)
    except batch.BatchError as e:
        return _err(e)
    resp = send_file(path, mimetype="application/zip", as_attachment=True,
                     download_name=f"batch_{batch_id[:8]}.zip")
    resp.call_on_close(lambda: path.unlink(missing_ok=True))
    return resp
