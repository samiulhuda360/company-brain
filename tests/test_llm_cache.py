import json
from pathlib import Path
from typing import Any

import pytest

from company_brain.llm import LLM, CacheMiss, CallCapReached


class FakeLLM(LLM):
    def __init__(self, cache: Path, **kw: Any) -> None:
        super().__init__(api_key="test", cache_dir=cache, offline=False, **kw)
        self.posts = 0

    def _post(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        self.posts += 1
        if endpoint == "embeddings":
            return {"data": [{"index": i, "embedding": [0.1, 0.2]} for i, _ in enumerate(payload["input"])]}, 0.2
        return {"choices": [{"message": {"content": json.dumps({"n": self.posts})}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, 0.5


def test_replies_are_recorded_and_replayed(tmp_path: Path) -> None:
    live = FakeLLM(tmp_path)
    first = live.chat_json("sys", "hello")
    again = live.chat_json("sys", "hello")
    assert not first.cache_hit and again.cache_hit and first.content == again.content and live.posts == 1
    offline = LLM(cache_dir=tmp_path, offline=True)
    assert offline.chat_json("sys", "hello").content == first.content
    with pytest.raises(CacheMiss):
        offline.chat_json("sys", "something new")
    vecs, _lat, hit = live.embed(["a", "b"])
    assert vecs == [[0.1, 0.2], [0.1, 0.2]] and not hit
    assert offline.embed(["a", "b"])[0] == vecs


def test_live_call_budget_is_enforced(tmp_path: Path) -> None:
    live = FakeLLM(tmp_path, max_live_calls=2)
    live.chat_json("s", "1")
    live.chat_json("s", "2")
    with pytest.raises(CallCapReached):
        live.chat_json("s", "3")
    assert live.chat_json("s", "1").cache_hit  # recorded replies still work after the cap
    assert live.live_calls_used() == 2


def test_no_key_means_offline() -> None:
    assert LLM(api_key="").offline
