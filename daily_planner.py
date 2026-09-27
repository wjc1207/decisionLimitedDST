"""Asynchronous, advisory daily planning for the fast JEV action loop."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional


DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
PLAN_PREVIEW_CHARS = 240
KNOWLEDGE_PATH = Path(__file__).resolve().with_name("DSTKnowledge.txt")
MAX_KNOWLEDGE_CHARS = 12000
PLAN_MAX_TOKENS = 4096


def planning_slot(state: dict) -> Optional[tuple[int, int]]:
    """Return (game day, zero-based quarter) from normalized DST world time."""
    world = state.get("world", {})
    day = world.get("day")
    if not isinstance(day, int) or day < 1:
        return None
    try:
        progress = float(world.get("time", 0))
    except (TypeError, ValueError):
        progress = 0.0
    if not math.isfinite(progress):
        progress = 0.0
    return day, min(3, max(0, int(progress * 4)))


def load_dst_knowledge(path: Path = KNOWLEDGE_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()[:MAX_KNOWLEDGE_CHARS]


def planning_context(state: dict, history: list[dict]) -> dict:
    """Summarize facts and recent action outcomes, not the repeating telemetry log."""
    player = state.get("player", {})
    inventory: dict[str, int] = {}
    for item in player.get("inventory", {}).get("items", []) + player.get("inventory", {}).get("equipped", []):
        prefab = str(item.get("prefab", "unknown"))
        inventory[prefab] = inventory.get(prefab, 0) + int(item.get("count", 1))
    nearby = sorted(state.get("nearby", []), key=lambda item: float(item.get("distance", 999)))
    return {
        "world": state.get("world", {}),
        "vitals": player.get("vitals", {}),
        "inventory": inventory,
        "crafting": state.get("crafting", {}),
        "navigation": state.get("navigation", {}),
        "nearby": [
            {
                "prefab": entity.get("prefab"),
                "distance": entity.get("distance"),
                "activeThreat": entity.get("activeThreat", False),
                "pickable": entity.get("pickable"),
            }
            for entity in nearby[:20]
        ],
        "science_machine_built_this_run": any(
            event.get("action") == "build_sciencemachine" and event.get("outcome") == "executed"
            for event in history
        ),
        "recent_actions": history[-40:],
    }


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def validate_plan(raw: object, day: int) -> dict:
    """Keep only a small, well-formed advisory plan in JEV's state."""
    if not isinstance(raw, dict):
        raise ValueError("DeepSeek plan must be a JSON object")
    goal = _short_text(raw.get("goal"), 160)
    if not goal:
        raise ValueError("DeepSeek plan has no goal")
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise ValueError("DeepSeek plan has no steps array")
    cleaned_steps = [_short_text(step, 120) for step in steps[:4]]
    cleaned_steps = [step for step in cleaned_steps if step]
    if not cleaned_steps:
        raise ValueError("DeepSeek plan has no usable steps")
    return {
        "day": day,
        "goal": goal,
        "steps": cleaned_steps,
        "priority": _short_text(raw.get("priority"), 160),
        "replan_if": [
            text for condition in (raw.get("replan_if") or [])[:3]
            if (text := _short_text(condition, 100))
        ] if isinstance(raw.get("replan_if"), list) else [],
    }


def request_plan(api_url: str, api_key: str, model: str, day: int, context: dict) -> dict:
    system_prompt = (
        "You are the daily planner for a Don't Starve Together agent. The separate JEV model "
        "makes fast next-action decisions; do not select or force its next action. Plan only "
        "a short, feasible goal for the next quarter of this game day using observed facts, "
        "the supplied DSTKnowledge reference, and recent outcomes. "
        "The current longer-term milestone is to build a science machine; if the supplied "
        "history says it was already built, do not plan to build another. Survival, food, "
        "light, and safety outrank optional progress. Do not assume unseen "
        "resources or unsupported abilities. The live state and hard safety rules always override "
        "your plan. Return JSON only, shaped like: "
        '{"goal":"...","steps":["...","..."],"priority":"...","replan_if":["..."]}'
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": "low",
        "max_tokens": PLAN_MAX_TOKENS,
        "stream": False,
    }
    for attempt in range(2):
        if attempt:
            # A short non-thinking retry leaves more of the budget for JSON output.
            body["reasoning_effort"] = "none"
        request = urllib.request.Request(
            api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek planning HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek planning connection failed: {exc.reason}") from exc

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek planning response has no choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content") or ""
        completion_tokens = (payload.get("usage") or {}).get("completion_tokens")
        if finish_reason == "stop" and content:
            try:
                return validate_plan(json.loads(content), day)
            except (ValueError, TypeError) as exc:
                reason = f"invalid_json_or_plan:{type(exc).__name__}"
        else:
            reason = f"finish_reason={finish_reason} content_chars={len(content)}"
        retryable = finish_reason in {"stop", "length", "insufficient_system_resource", "aborted"}
        if attempt == 0 and retryable:
            print(f"daily_plan_retry day={day} reason={reason} attempt=2/2")
            continue
        raise RuntimeError(
            f"DeepSeek planning response unusable: {reason} "
            f"completion_tokens={completion_tokens}"
        )
    raise RuntimeError("DeepSeek planning retry exhausted")


def timed_request_plan(api_url: str, api_key: str, model: str, day: int, context: dict) -> tuple:
    """Measure the worker's request, excluding the JEV cycle's polling delay."""
    started_at = time.monotonic()
    try:
        plan = request_plan(api_url, api_key, model, day, context)
    except Exception as exc:
        return None, time.monotonic() - started_at, exc
    return plan, time.monotonic() - started_at, None


class DailyPlanner:
    def __init__(
        self, api_key: str, api_url: str = DEFAULT_API_URL, model: str = DEFAULT_MODEL,
        knowledge_path: Path = KNOWLEDGE_PATH,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.knowledge_path = knowledge_path
        self.history: deque[dict] = deque(maxlen=80)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dst-planner")
        self.future: Optional[Future] = None
        self.pending_slot: Optional[tuple[int, int]] = None
        self.requested_slot: Optional[tuple[int, int]] = None
        self.plan: Optional[dict] = None
        self.science_machine_built = False
        self.failed_action: Optional[str] = None
        self.failure_count = 0
        self.last_replan = 0.0

    def update(self, state: dict) -> Optional[dict]:
        """Poll without waiting; schedule at most one plan per observed day-quarter."""
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        if self.plan is not None and (
            self.plan["day"] != day or self.plan.get("quarter") != quarter + 1
        ):
            self.plan = None
        if self.future is not None and self.future.done():
            future = self.future
            completed_slot = self.pending_slot
            self.future = None
            self.pending_slot = None
            try:
                result, response_seconds, error = future.result()
            except Exception as exc:
                # Planning is optional; even an unexpected planner failure must not stop JEV.
                print(f"daily_plan_error={exc}")
            else:
                if error is not None:
                    print(f"daily_plan_error response_time={response_seconds:.2f}s error={error}")
                elif completed_slot == slot and self.requested_slot == slot and result["day"] == day:
                    result["quarter"] = quarter + 1
                    self.plan = result
                    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    preview = serialized[:PLAN_PREVIEW_CHARS]
                    if len(serialized) > PLAN_PREVIEW_CHARS:
                        preview += "…"
                    print(
                        f"daily_plan_ready day={day} quarter={quarter + 1}/4 "
                        f"response_time={response_seconds:.2f}s preview={preview}"
                    )

        if self.requested_slot != slot and self.future is None:
            self.requested_slot = slot
            context = planning_context(state, list(self.history))
            context["science_machine_built_this_run"] = self.science_machine_built
            context["planning_window"] = {"day": day, "quarter": quarter + 1, "quarters_per_day": 4}
            try:
                context["dst_knowledge"] = load_dst_knowledge(self.knowledge_path)
            except (OSError, UnicodeError) as exc:
                context["dst_knowledge"] = ""
                print(f"daily_plan_knowledge_unavailable={exc}")
            self.future = self.executor.submit(
                timed_request_plan, self.api_url, self.api_key, self.model, day, context
            )
            self.pending_slot = slot
            print(
                f"daily_plan_requested day={day} quarter={quarter + 1}/4 "
                f"knowledge_chars={len(context['dst_knowledge'])}"
            )
        return self.plan if self.plan and self.plan["day"] == day else None

    def record_action(self, state: dict, action: str, outcome: str) -> None:
        vitals = state.get("player", {}).get("vitals", {})
        self.history.append({
            "day": state.get("world", {}).get("day"),
            "phase": state.get("world", {}).get("phase"),
            "action": action,
            "outcome": outcome,
            "hunger": vitals.get("hunger"),
            "health": vitals.get("health"),
        })
        if outcome == "failed":
            self.failure_count = self.failure_count + 1 if self.failed_action == action else 1
            self.failed_action = action
        else:
            self.failure_count = 0
            self.failed_action = None
        major_change = outcome == "executed" and action == "build_sciencemachine"
        if major_change:
            self.science_machine_built = True
        repeated_failure = self.failure_count >= 2
        if (major_change or repeated_failure) and time.monotonic() - self.last_replan >= 60:
            self.last_replan = time.monotonic()
            self.requested_slot = None
            self.plan = None
            self.failure_count = 0

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
