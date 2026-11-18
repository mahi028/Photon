"""Run claims are atomic across processes sharing sessions.db (Flask vs MCP)."""
import json
import os
import sqlite3
import subprocess
import sys

from src.backend.core.session import store
from src.backend.core.session.manager import get_manager

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _other_process_claim(sid):
    code = (f"import sys, json; sys.path.insert(0, {REPO!r})\n"
            "from src.backend.core.session.manager import get_manager\n"
            f"m = get_manager(); m.get_session({sid!r})\n"
            f"print(json.dumps([m.try_start_run({sid!r}), m.get_session({sid!r}).status]))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cross_process_claim(make_user):
    _, uid, _ = make_user()
    m = get_manager()
    sid = m.create_session(uid, [], "xproc").session_id
    m.get_session(sid)

    assert m.try_start_run(sid)
    assert _other_process_claim(sid) == [False, "running"]
    assert not m.try_start_run(sid)

    sess = m.get_session(sid)
    sess.title = "renamed"
    store.save_session(sess)                         # full-row write keeps status
    assert store.load_session_version(sid)[1] == "running"

    m.set_status(sid, "idle")
    assert _other_process_claim(sid) == [True, "running"]
    assert m.get_session(sid).status == "running"    # cache picks up the other process

    db = sqlite3.connect(store.DB_PATH)              # crashed run: claim expires
    db.execute("UPDATE sessions SET status_at = '2000-01-01T00:00:00' WHERE session_id = ?", (sid,))
    db.commit()
    assert m.try_start_run(sid)
    m.set_status(sid, "idle")
