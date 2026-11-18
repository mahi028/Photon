"""Sandbox: save-time AST policy + runtime guard. Attack targets are decoys
inside the throwaway volumes dir, never the repo (no real .env is read).

Runs through sandbox.backend, so PHOTON_SANDBOX=remote (+ URL/TOKEN) runs the
same attacks against a real sandbox worker (e.g. under gVisor)."""
import os
import tempfile

import pytest

from src.backend.config import config
from src.backend.core.sandbox import guard, limits
from src.backend.core.sandbox.subprocess_executor import ASTSecurityError, _check_ast_safety

PORTS = {"inputs": [{"name": "image", "type": "IMAGE_GRAY"}],
         "outputs": [{"name": "image", "type": "IMAGE_GRAY"}]}


@pytest.mark.parametrize("code", [
    "import os\ngetattr(os, 'sys' + 'tem')('true')",
    "import subprocess",
    "import os\nos.system('true')",
    "x = ().__class__.__bases__",
    "import numpy as np\nnp.load('a', allow_pickle=True)",
    "import torch\ntorch.load('m.pt')",
    "import pickle",
    "from . import x",
    "g = globals()",
])
def test_ast_rejects(code):
    with pytest.raises(ASTSecurityError):
        _check_ast_safety(code)


@pytest.mark.parametrize("code", [
    "def node(inputs, params):\n    open('x')",
    "def node(inputs, params):\n    import cv2\n    cv2.imread('x')",
    "def node(inputs, params):\n    import numpy as np\n    np.save('x', 1)",
])
def test_ast_strict_io_for_custom_nodes(code):
    _check_ast_safety(code.replace("open('x')", "pass").replace("cv2.imread('x')", "pass")
                      .replace("np.save('x', 1)", "pass"))          # fine without I/O
    with pytest.raises(ASTSecurityError):
        _check_ast_safety(code, strict_io=True)


def test_ast_allows_legit():
    _check_ast_safety("import numpy as np\nimport torch\nm = torch.load('m.pt', weights_only=True)\n"
                      "def main(i, o):\n    return np.zeros(3).sum()")


def test_thread_env_kept_by_guard():
    assert set(limits.THREAD_ENV_VARS) <= set(guard._SAFE_ENV)
    assert limits.thread_env()["OPENCV_FOR_THREADS_NUM"] == str(limits.thread_count())


# ---- runtime guard (spawns sandbox children) ----

@pytest.fixture(scope="module")
def decoys():
    secret = config.VOLUMES_DIR / "decoy_secret.txt"
    secret.write_text("TOP-SECRET")
    upload = config.UPLOADS_DIR / "decoy_other_user.png"
    upload.write_bytes(b"\x89PNG decoy")
    outside = config.VOLUMES_DIR / "pwned.txt"
    yield {"secret": str(secret), "upload": str(upload), "outside": str(outside),
           "db": str(config.VOLUMES_DIR / "sessions.db")}
    assert not outside.exists(), "sandbox wrote outside its roots"
    assert upload.exists(), "sandbox deleted another user's upload"


ATTACKS = {
    "read secret": "open({secret!r}).read()",
    "read via pathlib": "import pathlib; pathlib.Path({secret!r}).read_bytes()",
    "read sessions.db": "open({db!r}, 'rb').read(16)",
    "read other upload": "open({upload!r}, 'rb').read(16)",
    "read /etc/passwd": "open('/etc/passwd').read()",
    "write outside": "open({outside!r}, 'w').write('x')",
    "os.system via getattr": "import os; getattr(os, 'sys' + 'tem')('true')",
    "subprocess": "__import__('subprocess').run(['true'])",
    "fork": "import os; os.fork()",
    "posix_spawn": "import os; os.posix_spawn('/bin/true', ['true'], {{}})",
    "tcp": "import socket; socket.create_connection(('192.0.2.1', 80), 1)",
    "foreign unix socket": "import socket; s = socket.socket(socket.AF_UNIX); s.connect('/nonexistent.sock')",
    "ctypes": "import ctypes; ctypes.CDLL(None)",
    "pickle": "import pickle; pickle.loads(b'\\x80\\x04\\x95\\x0e\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x8c\\x02os\\x94\\x8c\\x06getcwd\\x94\\x93\\x94.')",
    "symlink then read": "import os; os.symlink({secret!r}, 'l'); open('l').read()",
    "hardlink": "import os; os.link({secret!r}, 'h')",
    "list volumes": "import os; os.listdir({vol!r})",
    "delete other upload": "import os; os.remove({upload!r})",
}


def _run_custom(code, disk_png):
    from src.backend.core.sandbox.backend import run_pipeline
    cn = {"cn1": {"custom_node_id": "cn1", "name": "t", "code": code, "ports": PORTS, "params": [], "doc": ""}}
    g = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}, {"id": "t", "custom_node_id": "cn1", "params": {}}],
         "edges": [{"from": ["input", "image"], "to": ["g", "image"]}, {"from": ["g", "gray"], "to": ["t", "image"]}]}
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    return run_pipeline(g, disk_png, out, out + "/nodes", custom_nodes=cn)


@pytest.mark.slow
@pytest.mark.parametrize("name", list(ATTACKS))
def test_guard_blocks_in_pipeline_child(name, decoys, disk_png):
    stmt = ATTACKS[name].format(vol=str(config.VOLUMES_DIR), **decoys)
    r = _run_custom(f"def node(inputs, params):\n    {stmt}\n    return {{'image': inputs['image']}}\n", disk_png)
    assert "Blocked by sandbox" in (r.get("error") or ""), r.get("error")
    assert "TOP-SECRET" not in str(r)


@pytest.mark.slow
def test_pipeline_child_env_and_legit_node(disk_png):
    r = _run_custom("def node(inputs, params):\n    import os\n    raise RuntimeError(sorted(os.environ))\n", disk_png)
    leaked = [k for k in ("FLASK_SECRET_KEY", "PHOTON_VOLUMES_DIR", "GEMINI_API_KEY") if k in r["error"]]
    assert not leaked
    r = _run_custom("def node(inputs, params):\n    import numpy as np\n    from skimage.filters import gaussian\n"
                    "    return {'image': gaussian(inputs['image'], 2, preserve_range=True).astype(np.uint8)}\n", disk_png)
    assert not r.get("error") and r.get("finals"), r.get("error")


def _run_code(code, disk_png):
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    from src.backend.core.sandbox.backend import get_executor
    return get_executor().execute(code, disk_png, out, timeout=60)


@pytest.mark.slow
def test_run_code_legit_and_blocked(disk_png, decoys):
    legit = ("import numpy as np, os, tifffile, cv2\nfrom PIL import Image\nfrom pathlib import Path\n"
             "def main(input_path, output_dir):\n"
             "    img = np.asarray(Image.open(input_path).convert('L'))\n"
             "    img = cv2.GaussianBlur(img, (31, 31), 5)\n"
             "    assert cv2.getNumThreads() <= {n}, cv2.getNumThreads()\n"
             "    os.makedirs(os.path.join(output_dir, 'sub'), exist_ok=True)\n"
             "    np.save(os.path.join(output_dir, 'sub', 'a.npy'), img)\n"
             "    tifffile.imwrite(os.path.join(output_dir, 'a.tif'), img)\n"
             "    p = Path(output_dir) / 'out.png'; Image.fromarray(255 - img).save(p)\n"
             "    return str(p)\n").replace("{n}", str(limits.thread_count()))
    r = _run_code(legit, disk_png)
    assert r.file_exists, (r.stderr[-500:], r.traceback)
    r = _run_code(legit.replace("return str(p)", f"open({decoys['secret']!r}).read(); return str(p)"), disk_png)
    assert "Blocked by sandbox" in (r.traceback or "") + r.stderr


@pytest.mark.slow
def test_pipeline_graph_with_bool_and_null_params(disk_png):
    """Regression: the job was embedded as a JSON literal, so `false`/`null`
    in params crashed the child with NameError before any node ran."""
    from src.backend.core.sandbox.backend import run_pipeline
    g = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}},
                   {"id": "o", "op": "otsu_threshold", "params": {"invert": False, "nbins": 256}, "note": None}],
         "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
                   {"from": ["g", "gray"], "to": ["o", "image"]}]}
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    r = run_pipeline(g, disk_png, out, out + "/nodes")
    assert not r.get("error") and r.get("finals"), r.get("error")
