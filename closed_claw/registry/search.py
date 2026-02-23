from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from closed_claw.config import Settings
from closed_claw.registry.store import SearchCandidate


@dataclass(slots=True)
class RerankedCandidate:
    agent_id: str
    score: float
    reason: str


class RerankerProtocol(Protocol):
    def rerank(self, task: str, candidates: list[SearchCandidate]) -> list[RerankedCandidate]:
        ...


class HeuristicReranker:
    def rerank(self, task: str, candidates: list[SearchCandidate]) -> list[RerankedCandidate]:
        task_terms = set(task.lower().split())
        out: list[RerankedCandidate] = []
        for cand in candidates:
            overlap = len(task_terms.intersection(set(cand.description.lower().split())))
            boosted = cand.score + (overlap * 0.02)
            out.append(
                RerankedCandidate(
                    agent_id=cand.agent_id,
                    score=min(1.0, boosted),
                    reason=f"semantic+term_overlap({overlap})",
                )
            )
        out.sort(key=lambda item: item.score, reverse=True)
        return out


class LLMReranker:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        timeout_sec: int,
        base_url: str,
        fallback: RerankerProtocol | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.base_url = base_url.rstrip("/")
        self.fallback = fallback or HeuristicReranker()

    def rerank(self, task: str, candidates: list[SearchCandidate]) -> list[RerankedCandidate]:
        if not candidates:
            return []
        try:
            text = self._call_provider(task, candidates)
            ranked = self._parse_output(text, candidates)
            if ranked:
                return ranked
        except Exception:
            pass

        out = self.fallback.rerank(task, candidates)
        return [RerankedCandidate(agent_id=c.agent_id, score=c.score, reason=f"llm_fallback:{c.reason}") for c in out]

    def _call_provider(self, task: str, candidates: list[SearchCandidate]) -> str:
        if self.provider == "openai":
            return self._call_openai(task, candidates)
        if self.provider == "gemini":
            return self._call_gemini(task, candidates)
        if self.provider == "claude":
            return self._call_claude(task, candidates)
        raise ValueError(f"unsupported llm provider: {self.provider}")

    def _prompt(self, task: str, candidates: list[SearchCandidate]) -> str:
        payload = [
            {"agent_id": c.agent_id, "semantic_score": c.score, "description": c.description}
            for c in candidates
        ]
        return (
            "You are selecting the best specialist agent for a task. "
            "Return JSON only with shape: {\"rankings\":[{\"agent_id\":str,\"score\":float,\"reason\":str}]}. "
            "Scores must be 0..1 and sorted descending.\n"
            f"Task: {task}\n"
            f"Candidates: {json.dumps(payload)}"
        )

    def _call_openai(self, task: str, candidates: list[SearchCandidate]) -> str:
        import httpx

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": self._prompt(task, candidates)}],
            "temperature": 0,
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _call_gemini(self, task: str, candidates: list[SearchCandidate]) -> str:
        import httpx

        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        body = {
            "contents": [{"parts": [{"text": self._prompt(task, candidates)}]}],
            "generationConfig": {"temperature": 0},
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, params=params, json=body)
            resp.raise_for_status()
            data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def _call_claude(self, task: str, candidates: list[SearchCandidate]) -> str:
        import httpx

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": 800,
            "temperature": 0,
            "messages": [{"role": "user", "content": self._prompt(task, candidates)}],
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        text_parts = [block.get("text", "") for block in data.get("content", []) if isinstance(block, dict)]
        return "\n".join(text_parts)

    def _parse_output(self, text: str, candidates: list[SearchCandidate]) -> list[RerankedCandidate]:
        payload = _extract_json(text)
        if isinstance(payload, dict):
            rankings = payload.get("rankings", [])
        elif isinstance(payload, list):
            rankings = payload
        else:
            rankings = []

        candidate_map = {c.agent_id: c for c in candidates}
        out: list[RerankedCandidate] = []
        for item in rankings:
            if not isinstance(item, dict):
                continue
            agent_id = str(item.get("agent_id", ""))
            if agent_id not in candidate_map:
                continue
            score = float(item.get("score", candidate_map[agent_id].score))
            out.append(
                RerankedCandidate(
                    agent_id=agent_id,
                    score=max(0.0, min(1.0, score)),
                    reason=str(item.get("reason", "llm")),
                )
            )

        used = {c.agent_id for c in out}
        for cand in candidates:
            if cand.agent_id not in used:
                out.append(
                    RerankedCandidate(
                        agent_id=cand.agent_id,
                        score=max(0.0, min(1.0, cand.score)),
                        reason="llm_backfill_semantic",
                    )
                )

        out.sort(key=lambda x: x.score, reverse=True)
        return out


def _extract_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))
    generic_match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if generic_match:
        return json.loads(generic_match.group(1))
    raise ValueError("LLM response did not contain valid JSON")


def build_reranker(settings: Settings) -> RerankerProtocol:
    provider = settings.llm_provider.lower()
    if provider == "heuristic":
        return HeuristicReranker()

    key = settings.llm_api_key.strip()
    if provider == "openai":
        key = key or settings.openai_api_key.strip()
        base = settings.openai_base_url
    elif provider == "gemini":
        key = key or settings.gemini_api_key.strip()
        base = settings.gemini_base_url
    elif provider == "claude":
        key = key or settings.anthropic_api_key.strip()
        base = settings.anthropic_base_url
    else:
        return HeuristicReranker()

    if not key:
        return HeuristicReranker()

    return LLMReranker(
        provider=provider,
        model=settings.llm_model,
        api_key=key,
        timeout_sec=settings.llm_timeout_sec,
        base_url=base,
        fallback=HeuristicReranker(),
    )


def generate_agent_description(settings: Settings, task: str) -> str:
    task = task.strip()
    heuristic = f"Specialist for tasks similar to: {task[:120]}"
    provider = settings.llm_provider.lower()
    if provider == "heuristic":
        return heuristic

    key = settings.llm_api_key.strip()
    if provider == "openai":
        key = key or settings.openai_api_key.strip()
        base = settings.openai_base_url.rstrip("/")
    elif provider == "gemini":
        key = key or settings.gemini_api_key.strip()
        base = settings.gemini_base_url.rstrip("/")
    elif provider == "claude":
        key = key or settings.anthropic_api_key.strip()
        base = settings.anthropic_base_url.rstrip("/")
    else:
        return heuristic
    if not key:
        return heuristic

    prompt = (
        "Write one concise agent description (max 20 words) for a specialist that handles this task. "
        "Return plain text only.\n"
        f"Task: {task}"
    )
    try:
        import httpx

        if provider == "openai":
            url = f"{base}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            body = {
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 60,
            }
            with httpx.Client(timeout=settings.llm_timeout_sec) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                return _clean_description(text, heuristic)

        if provider == "gemini":
            url = f"{base}/v1beta/models/{settings.llm_model}:generateContent"
            with httpx.Client(timeout=settings.llm_timeout_sec) as client:
                resp = client.post(
                    url,
                    params={"key": key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.2},
                    },
                )
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return _clean_description(text, heuristic)

        if provider == "claude":
            url = f"{base}/v1/messages"
            with httpx.Client(timeout=settings.llm_timeout_sec) as client:
                resp = client.post(
                    url,
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": settings.llm_model,
                        "max_tokens": 60,
                        "temperature": 0.2,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                parts = [
                    block.get("text", "")
                    for block in resp.json().get("content", [])
                    if isinstance(block, dict)
                ]
                return _clean_description(" ".join(parts), heuristic)
    except Exception:
        return heuristic

    return heuristic


def _clean_description(text: str, fallback: str) -> str:
    clean = " ".join((text or "").strip().split())
    if not clean:
        return fallback
    return clean[:200]
