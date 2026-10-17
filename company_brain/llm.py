"""OpenAI-compatible chat and embedding client with a record/replay cache, a rate limit and a hard call cap.

Every live reply is stored under ``cache/llm`` keyed by the SHA-256 of the exact request. The pipeline and the
evaluation are deterministic, so a replay sends identical requests and finds every reply in the cache: the
evaluation, the demo and CI all run offline, for anyone, with no key.

Configuration (environment): ``AI_API_KEY``, ``AI_BASE_URL``, ``AI_MODEL``, ``AI_EMBED_MODEL``, ``AI_MIN_INTERVAL``
(seconds between live calls), ``AI_MAX_CALLS`` (live-call budget, default 150) and ``BRAIN_OFFLINE=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-flash-lite-latest"
DEFAULT_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "cache" / "llm"


class CacheMiss(RuntimeError):
    """Offline, and this request has no recorded reply."""


class RateLimited(RuntimeError):
    """The provider returned 429. The run stops so that no result is invented."""


class CallCapReached(RuntimeError):
    """The live-call budget recorded in the cache directory is used up."""


@dataclass
class ChatResult:
    content: str
    latency_s: float
    cache_hit: bool
    usage: dict[str, int]


class LLM:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        embed_model: str | None = None,
        cache_dir: Path | None = None,
        offline: bool | None = None,
        min_interval: float | None = None,
        max_live_calls: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("AI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") + "/"
        self.model = model or os.environ.get("AI_MODEL") or DEFAULT_MODEL
        self.embed_model = embed_model or os.environ.get("AI_EMBED_MODEL") or DEFAULT_EMBED_MODEL
        self.cache_dir = cache_dir or Path(os.environ.get("BRAIN_CACHE_DIR", DEFAULT_CACHE))
        env_offline = os.environ.get("BRAIN_OFFLINE", "") == "1"
        self.offline = offline if offline is not None else (env_offline or not self.api_key)
        self.min_interval = min_interval if min_interval is not None else float(os.environ.get("AI_MIN_INTERVAL", "0"))
        self.max_live_calls = max_live_calls if max_live_calls is not None else int(os.environ.get("AI_MAX_CALLS", "150"))
        self._last = 0.0
        self._lock = threading.Lock()

    # ----- cache -----
    @staticmethod
    def request_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _path(self, digest: str) -> Path:
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _counter(self) -> Path:
        return self.cache_dir / "_live_calls.json"

    def live_calls_used(self) -> int:
        p = self._counter()
        return int(json.loads(p.read_text())["count"]) if p.exists() else 0

    def has(self, payload: dict[str, Any]) -> bool:
        return self._path(self.request_hash(payload)).exists()

    def _record(self, payload: dict[str, Any], endpoint: str) -> tuple[dict[str, Any], bool]:
        digest = self.request_hash(payload)
        path = self._path(digest)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), True
        if self.offline:
            raise CacheMiss(f"no recorded reply for request {digest[:12]} and no live model configured")
        with self._lock:
            used = self.live_calls_used()
            if used >= self.max_live_calls:
                raise CallCapReached(f"live-call budget of {self.max_live_calls} reached")
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            data, latency = self._post(endpoint, payload)
            self._last = time.monotonic()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._counter().write_text(json.dumps({"count": used + 1}) + "\n")
        rec = {"request_sha256": digest, "endpoint": endpoint, "latency_s": round(latency, 3), "response": data}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        return rec, False

    # ----- calls -----
    def chat_json(self, system: str, user: str) -> ChatResult:
        """One chat completion that must return a JSON object."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        rec, hit = self._record(payload, "chat/completions")
        resp = rec["response"]
        content = resp["choices"][0]["message"].get("content") or ""
        u = resp.get("usage") or {}
        usage = {"prompt_tokens": int(u.get("prompt_tokens", 0)), "completion_tokens": int(u.get("completion_tokens", 0))}
        return ChatResult(content, float(rec["latency_s"]), hit, usage)

    def embed(self, texts: list[str]) -> tuple[list[list[float]], float, bool]:
        """Embed a batch of texts (one request). Vectors are stored rounded to 5 decimals."""
        payload: dict[str, Any] = {"model": self.embed_model, "input": texts, "dimensions": EMBED_DIM}
        rec, hit = self._record(payload, "embeddings")
        data = rec["response"]["data"]
        ordered = sorted(enumerate(data), key=lambda kv: kv[1].get("index", kv[0]))
        vecs = [[round(x, 5) for x in d["embedding"]] for _i, d in ordered]
        if not hit:  # shrink the recording: full-precision floats are not needed for cosine ranking
            rec["response"] = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]}
            self._path(rec["request_sha256"]).write_text(json.dumps(rec, separators=(",", ":")) + "\n", encoding="utf-8")
        return vecs, float(rec["latency_s"]), hit

    def _post(self, endpoint: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        minute_waits = 0
        attempt = 0
        while attempt < 3:
            t0 = time.monotonic()
            r = httpx.post(self.base_url + endpoint, json=payload, headers=headers, timeout=120.0)
            latency = time.monotonic() - t0
            if r.status_code == 429:
                # A per-minute limit clears by itself: wait as told and try again. A daily limit stops the run.
                delay = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
                if "PerDay" not in r.text and minute_waits < 6:
                    minute_waits += 1
                    time.sleep(float(delay.group(1)) + 2 if delay else 65)
                    continue
                raise RateLimited(f"429 from provider: {r.text[:2000]}")
            if r.status_code >= 500 and attempt < 2:
                attempt += 1
                time.sleep(5 * attempt)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"provider error {r.status_code}: {r.text[:300]}")
            out: dict[str, Any] = r.json()
            return out, latency
        raise RuntimeError("provider kept failing")
