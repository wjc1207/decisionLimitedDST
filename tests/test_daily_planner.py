"""Offline checks for strategic planning without a real API call."""

from __future__ import annotations

import json
import unittest
from concurrent.futures import Future
from unittest.mock import patch

import daily_planner
import jev_agent


def observed(day: int = 1, world_time: float = 0.1) -> dict:
    return {
        "world": {"day": day, "phase": "day", "time": world_time},
        "player": {
            "vitals": {"hunger": 100, "health": 150},
            "inventory": {"items": [{"prefab": "twigs", "count": 2}], "equipped": []},
        },
        "crafting": {"torch": True},
        "nearby": [],
    }


class FakeExecutor:
    def __init__(self, **_kwargs):
        self.calls = []

    def submit(self, func, *args):
        self.calls.append((func, args))
        future = Future()
        future.set_result((
            {"day": args[3], "goal": "Find gold", "steps": ["Explore safely"]},
            1.25,
            None,
        ))
        return future

    def shutdown(self, **_kwargs):
        pass


class PlannerTests(unittest.TestCase):
    def test_plan_is_short_validated_and_advisory(self) -> None:
        plan = daily_planner.validate_plan({
            "goal": "Gather materials", "steps": ["Find gold", "Build a science machine"],
            "priority": "Keep light ready", "replan_if": ["Goal complete"],
        }, 2)
        self.assertEqual(plan["day"], 2)
        compact = jev_agent.compact_game_state(observed(2), plan)
        self.assertEqual(compact["daily_plan"], plan)
        self.assertIn("not a command", " ".join(compact["decision_guidance"]))
        with self.assertRaises(ValueError):
            daily_planner.validate_plan({"goal": "", "steps": []}, 2)

    def test_request_uses_json_mode_and_validates_response(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"finish_reason": "stop", "message": {
                        "content": json.dumps({"goal": "Find gold", "steps": ["Explore"]})
                    }}]
                }).encode("utf-8")

        context = daily_planner.planning_context(observed(), [])
        context["dst_knowledge"] = "Torch: 2 cut grass + 2 twigs"
        with patch.object(daily_planner.urllib.request, "urlopen", return_value=Response()) as send:
            plan = daily_planner.request_plan(
                "https://example.invalid/chat/completions", "secret", "deepseek-flash", 1,
                context,
            )
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertIn("Torch: 2 cut grass", body["messages"][1]["content"])
        self.assertEqual(plan["goal"], "Find gold")

    def test_length_truncation_retries_once_with_output_focused_mode(self) -> None:
        class Response:
            def __init__(self, finish_reason: str, content: str):
                self.payload = {
                    "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
                    "usage": {"completion_tokens": 2048},
                }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        completed = json.dumps({"goal": "Collect gold", "steps": ["Mine rocks"]})
        with (
            patch.object(daily_planner.urllib.request, "urlopen", side_effect=[
                Response("length", '{"goal":"Collect'), Response("stop", completed),
            ]) as send,
            patch("builtins.print") as printed,
        ):
            plan = daily_planner.request_plan("https://example.invalid/chat", "key", "model", 3, {})

        self.assertEqual(plan["goal"], "Collect gold")
        self.assertEqual(send.call_count, 2)
        first = json.loads(send.call_args_list[0].args[0].data)
        second = json.loads(send.call_args_list[1].args[0].data)
        self.assertEqual(first["max_tokens"], daily_planner.PLAN_MAX_TOKENS)
        self.assertEqual(first["reasoning_effort"], "low")
        self.assertEqual(second["reasoning_effort"], "none")
        self.assertIn("finish_reason=length", printed.call_args.args[0])

    def test_nonretryable_finish_reason_is_reported(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"choices":[{"finish_reason":"content_filter","message":{"content":""}}]}'

        with patch.object(daily_planner.urllib.request, "urlopen", return_value=Response()) as send:
            with self.assertRaisesRegex(RuntimeError, "finish_reason=content_filter"):
                daily_planner.request_plan("https://example.invalid/chat", "key", "model", 3, {})
        send.assert_called_once()

    def test_jev_receives_plan_as_state_not_as_action_filter(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"answers":{"next_action":{"choice":"wait","confidence":1}}}'

        choices = {"wait": "Wait briefly", "craft_torch": "Craft a torch"}
        plan = {"day": 1, "goal": "Find gold", "steps": ["Explore safely"]}
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=Response()) as send:
            jev_agent.call_jev("https://example.invalid/jev", "secret", "jev", observed(), choices, plan)
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(json.loads(body["state"])["daily_plan"], plan)
        self.assertEqual(body["questions"]["next_action"]["criteria"], choices)

    def test_daily_request_is_nonblocking_and_replans_after_day_change(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        self.assertIsNone(planner.update(observed(1)))
        with patch("builtins.print") as printed:
            self.assertEqual(planner.update(observed(1))["goal"], "Find gold")
        ready_line = printed.call_args.args[0]
        self.assertIn("response_time=1.25s", ready_line)
        self.assertIn('preview={"day":1,"goal":"Find gold"', ready_line)
        self.assertEqual(len(planner.executor.calls), 1)
        self.assertIsNone(planner.update(observed(2)))
        self.assertEqual(planner.update(observed(2))["day"], 2)
        self.assertEqual(len(planner.executor.calls), 2)
        planner.close()

    def test_quarter_day_boundaries_schedule_once_per_window(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        for progress, expected_calls in (
            (0.00, 1), (0.24, 1), (0.25, 2), (0.49, 2),
            (0.50, 3), (0.74, 3), (0.75, 4), (0.99, 4),
        ):
            with self.subTest(progress=progress):
                current = observed(world_time=progress)
                planner.update(current)
                planner.update(current)
                self.assertEqual(len(planner.executor.calls), expected_calls)
                self.assertEqual(planner.plan["quarter"], expected_calls)
        planner.close()

    def test_planning_context_includes_local_knowledge(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        planner.update(observed(world_time=0.51))
        context = planner.executor.calls[0][1][-1]
        self.assertEqual(context["planning_window"], {
            "day": 1, "quarter": 3, "quarters_per_day": 4,
        })
        self.assertIn("Science Machine", context["dst_knowledge"])
        self.assertIn("gold nugget + 4 logs + 4 rocks", context["dst_knowledge"])
        planner.close()

    def test_late_previous_quarter_plan_is_discarded(self) -> None:
        class PendingExecutor:
            def __init__(self, **_kwargs):
                self.futures = []

            def submit(self, _func, *_args):
                future = Future()
                self.futures.append(future)
                return future

            def shutdown(self, **_kwargs):
                pass

        with patch.object(daily_planner, "ThreadPoolExecutor", PendingExecutor):
            planner = daily_planner.DailyPlanner("secret")
        planner.update(observed(world_time=0.24))
        self.assertIsNone(planner.update(observed(world_time=0.25)))
        self.assertEqual(len(planner.executor.futures), 1)
        planner.executor.futures[0].set_result((
            {"day": 1, "goal": "Old plan", "steps": ["Wait"]}, 0.5, None,
        ))
        self.assertIsNone(planner.update(observed(world_time=0.25)))
        self.assertEqual(len(planner.executor.futures), 2)
        planner.executor.futures[1].set_result((
            {"day": 1, "goal": "Current plan", "steps": ["Explore"]}, 0.5, None,
        ))
        self.assertEqual(planner.update(observed(world_time=0.25))["goal"], "Current plan")
        planner.close()

    def test_timing_is_measured_in_worker(self) -> None:
        with (
            patch.object(daily_planner, "request_plan", return_value={"goal": "Find gold"}),
            patch.object(daily_planner.time, "monotonic", side_effect=[10.0, 10.4]),
        ):
            plan, elapsed, error = daily_planner.timed_request_plan("url", "key", "model", 1, {})
        self.assertEqual(plan["goal"], "Find gold")
        self.assertAlmostEqual(elapsed, 0.4)
        self.assertIsNone(error)

    def test_plan_preview_is_truncated(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        planner.update(observed())
        with patch.object(daily_planner, "PLAN_PREVIEW_CHARS", 20), patch("builtins.print") as printed:
            planner.update(observed())
        preview = printed.call_args.args[0].split("preview=", 1)[1]
        self.assertEqual(len(preview), 21)
        self.assertTrue(preview.endswith("…"))
        planner.close()

    def test_failed_action_is_recorded_for_next_plan(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        planner.update(observed())
        planner.update(observed())
        planner.record_action(observed(), "explore_other_region", "failed")
        planner.record_action(observed(), "explore_other_region", "failed")
        self.assertIsNone(planner.update(observed()))
        self.assertEqual(len(planner.executor.calls), 2)
        context = planner.executor.calls[-1][1][-1]
        self.assertEqual(context["recent_actions"][-1]["outcome"], "failed")
        planner.close()

    def test_science_machine_completion_is_preserved_in_later_plans(self) -> None:
        with patch.object(daily_planner, "ThreadPoolExecutor", FakeExecutor):
            planner = daily_planner.DailyPlanner("secret")
        planner.update(observed())
        planner.update(observed())
        planner.record_action(observed(), "build_sciencemachine", "executed")
        planner.update(observed(world_time=0.26))
        context = planner.executor.calls[-1][1][-1]
        self.assertTrue(context["science_machine_built_this_run"])
        planner.close()
