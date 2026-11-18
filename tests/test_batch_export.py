"""Batch output folders (D-050): folder checks and per-user defaults."""
import pytest

from src.backend.config import config
from src.backend.core.pipeline import batch_export as bx


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("PHOTON_OUTPUT_ROOTS", raising=False)
    monkeypatch.delenv("PHOTON_OUTPUT_DIR", raising=False)


def test_any_folder_without_roots(tmp_path):
    assert bx.resolve(None) is None                       # nothing chosen: no export
    assert bx.resolve("") is None                         # explicit "": no export
    d = tmp_path / "a" / "b"
    assert bx.resolve(str(d)) == d.resolve() and d.is_dir()


def test_refusals(tmp_path):
    with pytest.raises(bx.ExportError, match="absolute"):
        bx.resolve("relative/dir")
    with pytest.raises(bx.ExportError, match="data folder"):
        bx.resolve(str(config.VOLUMES_DIR / "outputs"))
    with pytest.raises(bx.ExportError, match="install folder"):
        bx.resolve(str(config.ROOT_DIR / "src"))
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(bx.ExportError, match="not a folder"):
        bx.resolve(str(f))


def test_operator_roots(monkeypatch, tmp_path):
    root = tmp_path / "out"
    monkeypatch.setenv("PHOTON_OUTPUT_ROOTS", str(root))
    assert bx.resolve(str(root / "x")) == (root / "x").resolve()
    for bad in (str(tmp_path / "elsewhere"), str(root / ".." / "x")):
        with pytest.raises(bx.ExportError, match="outside"):
            bx.resolve(bad)
    (root / "link").symlink_to(tmp_path)                  # symlink out of the root
    with pytest.raises(bx.ExportError, match="outside"):
        bx.resolve(str(root / "link" / "x"))


def test_job_dir_name(tmp_path):
    assert bx.job_dir(tmp_path, "NISL 535 / GM-WM", "abcdef0123456789").name == "NISL_535___GM-WM_abcdef01"
