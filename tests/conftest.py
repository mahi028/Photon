"""Shared fixtures. Every test runs against a throwaway volumes/ dir
(PHOTON_VOLUMES_DIR) and never loads the repo's .env, so the suite can't
touch real users, uploads or secrets.

Run:  uv run pytest            (all)
      uv run pytest -m "not slow"   (skip sandbox / MCP-server tests)
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

# Must be set before anything imports src.backend.config.
_VOL = tempfile.mkdtemp(prefix="photon-test-")
os.environ.update({
    "PHOTON_VOLUMES_DIR": _VOL,
    "PHOTON_NO_DOTENV": "1",
    "MODEL_SIDECAR_ENABLED": "0",
    "PHOTON_SIGNUP": "open",
    "FLASK_SECRET_KEY": "test-secret",
    "FLASK_ENV": "testing",
    "MODEL_SIDECAR_SOCKET": os.path.join(tempfile.gettempdir(), f"photon-test-{uuid.uuid4().hex[:8]}.sock"),
})
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from src.backend.config import config  # noqa: E402

assert str(config.VOLUMES_DIR) == str(Path(_VOL).resolve()), "tests must not use the real volumes/"


@pytest.fixture(scope="session")
def app():
    from src.backend.app import create_app
    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def make_user(app):
    """make_user() -> (logged-in test client, user_id, username)."""
    def _make():
        c = app.test_client()
        u = "t" + uuid.uuid4().hex[:10]
        r = c.post("/auth/signup", json={"username": u, "email": f"{u}@example.org",
                                         "password": "password123"})
        assert r.status_code == 201, r.get_json()
        return c, r.get_json()["user_id"], u
    return _make


@pytest.fixture(scope="session")
def disk_png(tmp_path_factory) -> str:
    """A small RGB test image: bright disk on a dark background."""
    yy, xx = np.mgrid[:96, :128]
    disk = ((yy - 48) ** 2 + (xx - 64) ** 2 < 30 ** 2).astype(np.uint8) * 200 + 20
    p = tmp_path_factory.mktemp("img") / "disk.png"
    Image.fromarray(np.stack([disk, disk // 2, 255 - disk], -1)).save(p)
    return str(p)


@pytest.fixture
def upload(disk_png):
    """upload(client) -> image_id"""
    def _up(c):
        with open(disk_png, "rb") as f:
            r = c.post("/api/upload", data={"file": (f, "disk.png")}, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        return r.get_json()["image_id"]
    return _up
