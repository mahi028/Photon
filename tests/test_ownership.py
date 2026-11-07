"""Images, previews and outputs are only reachable by their owner."""
import sqlite3

from src.backend.config import config
from src.backend.core.session import store


def test_file_routes_and_image_access(app, make_user, upload):
    A, _, _ = make_user()
    B, _, _ = make_user()
    anon = app.test_client()
    img = upload(A)
    prev = f"/previews/uploads/{img}_preview.jpg"
    sid = A.post("/api/sessions", json={"image_ids": [img]}).get_json()["session_id"]
    out = config.OUTPUTS_DIR / sid / "run1"
    out.mkdir(parents=True)
    (out / "o.png").write_bytes(b"\x89PNG fake")

    # The old route served anything under volumes/, sessions.db included.
    assert anon.get("/previews/sessions.db").status_code == 404
    assert anon.get(prev).status_code == 302                       # login redirect
    assert anon.post("/api/llm/model", json={"model": "x"}).status_code == 401

    assert A.get(f"/api/images/{img}").status_code == 200
    assert A.get(prev).status_code == 200
    assert A.get(f"/outputs/{sid}/run1/o.png").status_code == 200

    assert B.get(f"/api/images/{img}").status_code == 404
    assert B.get(prev).status_code == 404
    assert B.get(f"/outputs/{sid}/run1/o.png").status_code == 404
    assert B.get("/previews/uploads/..%2Fsessions.db").status_code == 404
    assert B.post("/api/sessions", json={"image_ids": [img]}).status_code == 404
    bsid = B.post("/api/sessions", json={}).get_json()["session_id"]
    assert B.post(f"/api/sessions/{bsid}/images", json={"image_id": img}).status_code == 404

    # Images from before owner_id existed stay reachable through their session.
    db = sqlite3.connect(store.DB_PATH)
    db.execute("UPDATE images SET owner_id = NULL WHERE image_id = ?", (img,))
    db.commit()
    assert A.get(f"/api/images/{img}").status_code == 200
    assert B.get(f"/api/images/{img}").status_code == 404
