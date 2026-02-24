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


def generate_agent_profile(
    settings: Settings,
    task: str,
    supported_tools: list[str],
    fallback_tools: list[str],
) -> dict[str, Any]:
    provider = settings.llm_provider.lower()
    key, base = _provider_key_and_base(settings, provider)
    if provider == "heuristic" or not key:
        return _heuristic_agent_profile(task, supported_tools, fallback_tools)

    prompt = (
        "You create reusable capability profiles for specialist agents.\n"
        "Given a user task, return JSON only with keys:\n"
        "profile_id, name_prefix, description, tools_allowlist, tags, skill_md, api_capabilities, requires_approval_for.\n"
        f"Allowed tools: {json.dumps(supported_tools)}\n"
        "Rules:\n"
        "- profile must be reusable and capability-oriented, not a one-off task label.\n"
        "- name_prefix should be concise (2-5 words).\n"
        "- description max 28 words.\n"
        "- tags should include 'auto' and 'capability'.\n"
        "- skill_md should be a short markdown role guide.\n"
        f"Task: {task.strip()}"
    )
    try:
        text = _generate_text_with_provider(
            provider=provider,
            model=settings.llm_model,
            api_key=key,
            base_url=base,
            timeout_sec=settings.llm_timeout_sec,
            prompt=prompt,
            max_tokens=500,
            temperature=0.2,
        )
        payload = _extract_json(text)
        if isinstance(payload, dict):
            profile = _normalize_profile_payload(
                payload=payload,
                task=task,
                supported_tools=supported_tools,
                fallback_tools=fallback_tools,
            )
            if profile:
                return profile
    except Exception:
        pass
    return _heuristic_agent_profile(task, supported_tools, fallback_tools)


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


def generate_task_plan(settings: Settings, task: str) -> list[dict[str, Any]]:
    provider = settings.llm_provider.lower()
    key, base = _provider_key_and_base(settings, provider)
    if provider == "heuristic" or not key:
        return _heuristic_task_plan(task)

    prompt = (
        "You are a planning supervisor for multi-agent execution.\n"
        "Return JSON only with shape:\n"
        "{\"subtasks\":[{\"task_id\":str,\"title\":str,\"description\":str,\"role_tag\":str,"
        "\"depends_on\":[str],\"acceptance_criteria\":[str],\"requires_tool\":bool}]}\n"
        "Rules:\n"
        "- Decompose into atomic tasks.\n"
        "- Prefer independent tasks; only include dependencies when unavoidable.\n"
        "- role_tag should be reusable capability labels (not person names).\n"
        "- each acceptance_criteria entry must be verifiable.\n"
        f"Task: {task.strip()}"
    )
    try:
        text = _generate_text_with_provider(
            provider=provider,
            model=settings.llm_model,
            api_key=key,
            base_url=base,
            timeout_sec=settings.llm_timeout_sec,
            prompt=prompt,
            max_tokens=800,
            temperature=0.1,
        )
        payload = _extract_json(text)
        if isinstance(payload, dict):
            tasks = _normalize_plan_payload(payload)
            if tasks:
                return tasks
    except Exception:
        pass
    return _heuristic_task_plan(task)


def _provider_key_and_base(settings: Settings, provider: str) -> tuple[str, str]:
    key = settings.llm_api_key.strip()
    if provider == "openai":
        key = key or settings.openai_api_key.strip()
        return key, settings.openai_base_url.rstrip("/")
    if provider == "gemini":
        key = key or settings.gemini_api_key.strip()
        return key, settings.gemini_base_url.rstrip("/")
    if provider == "claude":
        key = key or settings.anthropic_api_key.strip()
        return key, settings.anthropic_base_url.rstrip("/")
    return "", ""


def _generate_text_with_provider(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout_sec: int,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    import httpx

    if provider == "openai":
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    if provider == "gemini":
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature},
                },
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    if provider == "claude":
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            parts = [
                block.get("text", "")
                for block in resp.json().get("content", [])
                if isinstance(block, dict)
            ]
            return " ".join(parts)
    raise ValueError(f"unsupported provider: {provider}")


def _normalize_profile_payload(
    *,
    payload: dict[str, Any],
    task: str,
    supported_tools: list[str],
    fallback_tools: list[str],
) -> dict[str, Any]:
    raw_name = _clean_text(str(payload.get("name_prefix", "")), default="Task Operator", max_len=60)
    name_prefix = _to_title_words(raw_name) or "Task Operator"
    raw_desc = _clean_text(
        str(payload.get("description", "")),
        default=f"Capability operator for tasks like: {task.strip()[:140]}",
        max_len=220,
    )
    requested_tools = payload.get("tools_allowlist", [])
    tools = [t for t in requested_tools if isinstance(t, str) and t in supported_tools]
    if not tools:
        tools = [t for t in fallback_tools if t in supported_tools]

    requested_tags = payload.get("tags", [])
    tags = [t for t in requested_tags if isinstance(t, str) and t.strip()]
    tags = [_slug(t) for t in tags if _slug(t)]
    tags = list(dict.fromkeys(["auto", "capability", *tags]))

    profile_id = _slug(str(payload.get("profile_id", ""))) or _slug(name_prefix)
    skill_md = str(payload.get("skill_md", "")).strip()
    if not skill_md:
        skill_md = (
            f"# {name_prefix}\n\n"
            f"You are a reusable capability agent specialized in: {raw_desc}\n"
            "Execute requests safely and report concrete outcomes.\n"
        )
    elif not skill_md.startswith("#"):
        skill_md = f"# {name_prefix}\n\n{skill_md}"

    return {
        "profile_id": profile_id,
        "name_prefix": name_prefix,
        "description": raw_desc,
        "tools_allowlist": tools,
        "tags": tags,
        "skill_md": skill_md,
        "api_capabilities": [
            str(v).strip()
            for v in payload.get("api_capabilities", [])
            if isinstance(v, str) and str(v).strip()
        ] if isinstance(payload.get("api_capabilities", []), list) else [],
        "requires_approval_for": [
            str(v).strip()
            for v in payload.get("requires_approval_for", [])
            if isinstance(v, str) and str(v).strip()
        ] if isinstance(payload.get("requires_approval_for", []), list) else [],
    }


def _heuristic_agent_profile(task: str, supported_tools: list[str], fallback_tools: list[str]) -> dict[str, Any]:
    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", task.lower())
    uniq: list[str] = []
    for term in terms:
        if term in {"please", "could", "would", "should", "using", "with", "from", "that", "this"}:
            continue
        if term not in uniq:
            uniq.append(term)
        if len(uniq) >= 2:
            break
    topic = " ".join(uniq).strip() or "Task"
    name_prefix = f"{_to_title_words(topic)} Operator".strip()
    description = f"Reusable capability operator for {task.strip()[:140]}"
    tools = [t for t in fallback_tools if t in supported_tools] or ["terminal"]
    profile_id = _slug(name_prefix)
    return {
        "profile_id": profile_id,
        "name_prefix": name_prefix,
        "description": description,
        "tools_allowlist": tools,
        "tags": ["auto", "capability", profile_id],
        "skill_md": (
            f"# {name_prefix}\n\n"
            f"You are a capability-focused operator for tasks in this domain: {task.strip()}\n"
            "Prioritize safe execution, clear plans, and concrete result reporting.\n"
        ),
        "api_capabilities": [],
        "requires_approval_for": [],
    }


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return clean[:64]


def _to_title_words(value: str) -> str:
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", value) if p]
    return " ".join(p.capitalize() for p in parts[:5])


def _clean_text(value: str, default: str, max_len: int) -> str:
    clean = " ".join((value or "").strip().split())
    if not clean:
        clean = default
    return clean[:max_len]


def _normalize_plan_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("subtasks", [])
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        raw_id = _slug(str(item.get("task_id", ""))) or f"task-{i}"
        task_id = raw_id
        suffix = 2
        while task_id in seen_ids:
            task_id = f"{raw_id}-{suffix}"
            suffix += 1
        seen_ids.add(task_id)

        title = _clean_text(str(item.get("title", "")), f"Subtask {i}", 80)
        desc = _clean_text(str(item.get("description", "")), title, 260)
        role_tag = _slug(str(item.get("role_tag", ""))) or "general-operator"
        depends_on_raw = item.get("depends_on", [])
        depends_on = [
            _slug(str(dep))
            for dep in depends_on_raw
            if isinstance(dep, str) and _slug(str(dep))
        ] if isinstance(depends_on_raw, list) else []
        ac_raw = item.get("acceptance_criteria", [])
        criteria = [
            _clean_text(str(c), "", 160)
            for c in ac_raw
            if isinstance(c, str) and str(c).strip()
        ] if isinstance(ac_raw, list) else []
        if not criteria:
            criteria = ["Task output is complete, correct, and explicitly reported."]
        out.append(
            {
                "task_id": task_id,
                "title": title,
                "description": desc,
                "role_tag": role_tag,
                "depends_on": depends_on,
                "acceptance_criteria": criteria,
                "requires_tool": bool(item.get("requires_tool", False)),
            }
        )

    valid_ids = {t["task_id"] for t in out}
    for item in out:
        item["depends_on"] = [dep for dep in item["depends_on"] if dep in valid_ids and dep != item["task_id"]]
    return out


def _heuristic_task_plan(task: str) -> list[dict[str, Any]]:
    clean_task = _clean_text(task, "Execute task", 260)
    return [
        {
            "task_id": "task-1",
            "title": "Execute User Task",
            "description": clean_task,
            "role_tag": "general-operator",
            "depends_on": [],
            "acceptance_criteria": [
                "All explicit user requirements are addressed.",
                "Final output summarizes what was done and resulting artifacts.",
            ],
            "requires_tool": False,
        }
    ]
