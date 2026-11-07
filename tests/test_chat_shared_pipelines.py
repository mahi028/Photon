"""Photon's chat LLM sees shared pipelines and can run one by published_id.
The LLM is faked: send_turn returns scripted turns."""
import json
import time

import pytest

from src.backend.core.llm.prompts import _published_block
from src.backend.core.llm.schema import LLMTurnResult
from src.backend.core.session.manager import get_manager

GRAY = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}],
        "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}


def test_schema_accepts_published_reference():
    r = LLMTurnResult(response_type="pipeline", message="m", pipeline={"published_id": "abc"})
    assert r.pipeline["published_id"] == "abc"
    with pytest.raises(ValueError):
        LLMTurnResult(response_type="pipeline", message="m", pipeline={"edges": []})


def test_published_block_formats_and_caps():
    pubs = [{"published_id": f"id{i}", "title": "T\nitle", "latest_version": 2, "owner": "ann",
             "ops": ["to_grayscale", "custom"], "description": "x " * 200} for i in range(40)]
    block = _published_block(pubs)
    assert "`id0`" in block and "to_grayscale -> custom" in block and "by ann" in block
    assert "... and 10 more" in block
    assert _published_block([]) == "(none published yet)"


@pytest.mark.slow
def test_chat_runs_shared_pipeline(make_user, upload, monkeypatch):
    author, _, _ = make_user()
    c, _, _ = make_user()
    pid = author.post("/api/pipelines", json={"title": "Grayscale it", "graph": GRAY}).get_json()["pipeline_id"]
    pub = author.post(f"/api/pipelines/{pid}/publish", json={}).get_json()["published_id"]

    seen = {}
    turns = iter([
        LLMTurnResult(response_type="pipeline", message="using shared",
                      pipeline={"published_id": "does-not-exist"}),
        LLMTurnResult(response_type="pipeline", message="using shared", pipeline={"published_id": pub}),
        LLMTurnResult(response_type="chat", message="done"),
    ])

    def fake_send_turn(conversation, system_prompt):
        seen.setdefault("prompt", system_prompt)
        return next(turns)

    from src.backend.api import chat_routes
    monkeypatch.setattr(chat_routes, "send_turn", fake_send_turn)
    sid = c.post("/api/sessions", json={"image_ids": [upload(c)]}).get_json()["session_id"]
    assert c.post(f"/api/sessions/{sid}/message", json={"prompt": "make it gray"}).status_code == 202

    m = get_manager()
    for _ in range(200):
        time.sleep(0.25)
        m.invalidate(sid)
        sess = m.get_session(sid)
        if sess.status == "idle" and sess.outputs:
            break
    assert f"`{pub}`" in seen["prompt"] and "Grayscale it" in seen["prompt"]
    assert any("Shared pipeline not available" in (msg.message or "") for msg in sess.llm_conversation)
    prov = json.loads(sess.outputs[-1].code)
    assert prov["published_id"] == pub and prov["version"] == 1
