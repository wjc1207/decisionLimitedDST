"""Offline regressions for action exposure, target selection and completion."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, call, patch

import controller
import jev_agent
import telemetry


def grass(guid: int, distance: float, pickable: bool) -> dict:
    return {
        "guid": guid,
        "prefab": "grass",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["pickable"] if pickable else [],
        "pickable": pickable,
    }


def hostile(guid: int, distance: float) -> dict:
    return {
        "guid": guid,
        "prefab": "hound",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["hostile", "monster"],
        "attackable": True,
        "activeThreat": True,
    }


def lit_campfire(guid: int = 40, distance: float = 3.0) -> dict:
    return {
        "guid": guid,
        "prefab": "campfire",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["campfire", "fire", "cooker"],
        "attackable": False,
    }


def state(nearby: list[dict], cutgrass: int = 0) -> dict:
    items = []
    if cutgrass:
        items.append({"prefab": "cutgrass", "count": cutgrass})
    return {
        "world": {"day": 1, "phase": "day", "time": 0.2},
        "player": {
            "vitals": {"health": 150, "hunger": 140, "hunger_max": 150, "sanity": 200},
            "inventory": {"items": items, "equipped": []},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        },
        "crafting": {"torch": False, "axe": False, "pickaxe": False, "campfire": False},
        "nearby": nearby,
        "navigation": {
            "mode": "frontier",
            "status": "ready",
            "target": {
                "x": 0.0,
                "z": 12.0,
                "distance": 12.0,
                "information_gain": 4,
                "visits": 0,
                "score": 34.0,
            },
            "leg": {"x": 0.0, "z": 4.0, "dx": 0.0, "dz": 4.0, "distance": 4.0},
        },
        "camera": {
            "right": {"x": 1, "z": 0},
            "forward": {"x": 0, "z": 1},
        },
    }


class CandidateTests(unittest.TestCase):
    def test_critical_hunger_with_food_exposes_only_eating(self) -> None:
        observed = state([grass(10, 2.0, True)])
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [
            {"prefab": "carrot", "count": 9}, {"prefab": "berries", "count": 9}
        ]

        criteria, dispatch = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"eat_safe_food"})
        self.assertEqual(dispatch["eat_safe_food"], {"kind": "eat_safe_food"})

    def test_critical_hunger_is_checked_by_action_allowed(self) -> None:
        observed = state([grass(10, 2.0, True)])
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [{"prefab": "carrot", "count": 1}]

        self.assertTrue(jev_agent.action_allowed(observed, "eat_safe_food")[0])
        self.assertTrue(jev_agent.action_allowed(observed, "flee_from_nearest_hostile")[0])
        for action in ("wait", "collect", "explore", "cook_food"):
            with self.subTest(action=action):
                self.assertFalse(jev_agent.action_allowed(observed, action)[0])

    def test_critical_hunger_without_food_keeps_exploration_available(self) -> None:
        observed = state([])
        observed["player"]["vitals"]["hunger"] = 3

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertIn("explore_current_region", criteria)
        self.assertNotIn("eat_safe_food", criteria)

    def test_critical_hunger_at_night_keeps_light_options(self) -> None:
        observed = state([])
        observed["world"]["phase"] = "night"
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [
            {"prefab": "carrot", "count": 1}, {"prefab": "torch", "count": 1}
        ]

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"eat_safe_food", "equip_torch"})
        for action in ("equip_torch", "craft_torch", "build_campfire"):
            with self.subTest(action=action):
                self.assertTrue(jev_agent.action_allowed(observed, action)[0])

    def test_critical_hunger_near_lit_fire_allows_eating(self) -> None:
        observed = state([lit_campfire()])
        observed["world"]["phase"] = "night"
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [{"prefab": "carrot", "count": 1}]

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"eat_safe_food"})

    def test_cooked_meat_is_available_for_eating(self) -> None:
        for prefab in ("cookedmeat", "cookedsmallmeat"):
            with self.subTest(prefab=prefab):
                observed = state([])
                observed["player"]["vitals"]["hunger"] = 3
                observed["player"]["inventory"]["items"] = [{"prefab": prefab, "count": 1}]

                criteria, _ = jev_agent.build_candidates(observed)

                self.assertEqual(set(criteria), {"eat_safe_food"})
                self.assertEqual(controller.safe_food_count(observed), 1)

    def test_raw_meat_near_fire_exposes_cook_then_eat(self) -> None:
        observed = state([lit_campfire()])
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 1}]

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"eat_safe_food"})
        self.assertIn("cook one item", criteria["eat_safe_food"])

        observed["nearby"] = []
        self.assertNotIn("eat_safe_food", jev_agent.build_candidates(observed)[0])
        self.assertTrue(jev_agent.action_allowed(observed, "wait")[0])

    def test_critical_hunger_does_not_override_immediate_threat(self) -> None:
        observed = state([hostile(20, 2.0)])
        observed["player"]["vitals"]["hunger"] = 3
        observed["player"]["inventory"]["items"] = [{"prefab": "carrot", "count": 1}]

        for action in ("flee_from_nearest_hostile", "equip_weapon", "attack_nearest_hostile"):
            with self.subTest(action=action):
                self.assertTrue(jev_agent.action_allowed(observed, action)[0])
        self.assertFalse(jev_agent.action_allowed(observed, "eat_safe_food")[0])

    def test_prompt_describes_composite_actions_and_current_food_plan(self) -> None:
        observed = state([
            lit_campfire(),
            {"guid": 21, "prefab": "evergreen", "distance": 2.0,
             "tags": ["CHOP_workable"], "work_required": 6},
            {"guid": 22, "prefab": "rock1", "distance": 2.0,
             "tags": ["MINE_workable"], "work_required": 6},
        ])
        observed["player"]["vitals"]["hunger"] = 100
        observed["player"]["inventory"]["items"] = [
            {"prefab": "meat", "count": 1},
            {"prefab": "axe", "count": 1, "durability_percent": 1.0},
            {"prefab": "pickaxe", "count": 1, "durability_percent": 1.0},
        ]

        criteria, _ = jev_agent.build_candidates(observed)
        compact = jev_agent.compact_game_state(observed)

        self.assertIn("logs and pinecones", criteria["chop_nearest_tree"])
        self.assertIn("rocks, flint, nitre, and gold", criteria["mine_nearest_rock"])
        self.assertIn("cook one item first", criteria["eat_safe_food"])
        self.assertIn("prepare food for later", criteria["cook_food"])
        self.assertEqual(compact["hunger_fraction"], round(100 / 150, 3))
        self.assertEqual(compact["cookable_raw_count"], 1)
        self.assertTrue(compact["nearby_cooker"])
        self.assertFalse(any("unequip" in rule for rule in compact["hard_rules"]))

        observed["player"]["inventory"]["items"].append(
            {"prefab": "cookedmeat", "count": 1}
        )
        cooked_criteria, _ = jev_agent.build_candidates(observed)
        self.assertIn("directly", cooked_criteria["eat_safe_food"])

    def test_revised_prompt_is_forwarded_to_jev(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "answers": {"next_action": {"choice": "wait", "confidence": 1.0}}
                }).encode("utf-8")

        observed = state([])
        criteria, _ = jev_agent.build_candidates(observed)
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=FakeResponse()) as send:
            jev_agent.call_jev("https://example.invalid/jev", "test-key", "test-model", observed, criteria)

        request = send.call_args.args[0]
        body = json.loads(request.data)
        forwarded = json.loads(body["state"])
        self.assertEqual(set(body["questions"]["next_action"]["criteria"]), set(criteria))
        self.assertIn("bounded action", body["questions"]["next_action"]["instructions"])
        self.assertIn("decision_guidance", forwarded)
        self.assertNotIn("first_day_survival_knowledge", forwarded)

    def test_removed_knowledge_is_absent_on_both_days(self) -> None:
        for day in (1, 2):
            with self.subTest(day=day):
                observed = state([])
                observed["world"]["day"] = day
                observed["player"]["inventory"]["items"] = [
                    {"prefab": "twigs", "count": 1},
                    {"prefab": "cutgrass", "count": 2},
                ]
                self.assertNotIn("knowledge", jev_agent.compact_game_state(observed))

    def test_cooking_requires_lit_cooker_and_raw_food(self) -> None:
        ready = state([lit_campfire()])
        ready["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 2}]
        unlit = state([{"prefab": "campfire", "distance": 3, "tags": ["campfire", "cooker"]}])
        unlit["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 2}]
        already_cooked = state([lit_campfire()])
        already_cooked["player"]["inventory"]["items"] = [
            {"prefab": "cookedmeat", "count": 2}
        ]

        criteria, dispatch = jev_agent.build_candidates(ready)
        self.assertIn("cook_food", criteria)
        self.assertEqual(dispatch["cook_food"], {"kind": "cook_food"})
        self.assertNotIn("cook_food", jev_agent.build_candidates(unlit)[0])
        self.assertNotIn("cook_food", jev_agent.build_candidates(already_cooked)[0])

    def test_cooking_is_not_offered_outside_fire_range(self) -> None:
        observed = state([lit_campfire(distance=7)])
        observed["player"]["inventory"]["items"] = [{"prefab": "berries", "count": 2}]

        self.assertNotIn("cook_food", jev_agent.build_candidates(observed)[0])

    def test_explore_is_omitted_without_a_reachable_frontier(self) -> None:
        observed = state([])
        observed["navigation"] = {
            "mode": "frontier",
            "status": "no_frontier",
            "target": None,
            "leg": None,
        }

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertNotIn("explore_current_region", criteria)
        self.assertNotIn("explore_other_region", criteria)

    def test_night_campfire_requires_torch_for_travel_and_collection(self) -> None:
        observed = state([lit_campfire(), grass(10, 4.0, True)])
        observed["world"]["phase"] = "night"
        observed["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 4.0, "z": 0.0, "distance": 4.0},
            "leg": {"x": 4.0, "z": 0.0, "distance": 4.0},
        }

        criteria, _ = jev_agent.build_candidates(observed)

        for action in ("explore_current_region", "explore_other_region", "collect_grass"):
            self.assertNotIn(action, criteria)
        self.assertIn("wait", criteria)

        observed["player"]["inventory"]["equipped"] = [{"prefab": "torch", "count": 1}]
        criteria_with_torch, _ = jev_agent.build_candidates(observed)
        for action in ("explore_current_region", "explore_other_region", "collect_grass"):
            self.assertIn(action, criteria_with_torch)

    def test_region_explore_is_one_semantic_action(self) -> None:
        criteria, dispatch = jev_agent.build_candidates(state([]))

        self.assertIn("explore_current_region", criteria)
        self.assertNotIn("explore_other_region", criteria)
        self.assertNotIn("explore_up", criteria)
        self.assertEqual(dispatch["explore_current_region"]["kind"], "explore")
        self.assertEqual(dispatch["explore_current_region"]["navigation_mode"], "region")
        self.assertEqual(dispatch["explore_current_region"]["target_z"], 4.0)
        self.assertEqual(dispatch["explore_current_region"]["frontier_z"], 12.0)

    def test_observed_road_exposes_other_region_exploration(self) -> None:
        observed = state([])
        observed["navigation"]["current_region"] = "Forest:1"
        observed["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 4.0, "z": 0.0, "distance": 4.0},
            "leg": {"x": 4.0, "z": 0.0, "distance": 4.0},
        }

        criteria, dispatch = jev_agent.build_candidates(observed)

        self.assertIn("explore_current_region", criteria)
        self.assertIn("explore_other_region", criteria)
        self.assertIn("Forest:1", criteria["explore_other_region"])
        self.assertEqual(dispatch["explore_other_region"]["navigation_mode"], "road")

    def test_road_choice_dispatches_bounded_follow_action(self) -> None:
        observed = state([])
        observed["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 2.0, "z": 0.0, "distance": 2.0},
            "leg": {"x": 2.0, "z": 0.0, "distance": 2.0},
        }
        _, dispatch = jev_agent.build_candidates(observed)
        with patch.object(controller, "follow_road", return_value=True) as follow:
            moved = jev_agent.execute_action(
                dispatch["explore_other_region"], Path("unused.log"), move_seconds=1.2,
            )

        self.assertTrue(moved)
        follow.assert_called_once_with(Path("unused.log"), 2.0, 0.0, 2.0, 1.2)

    def test_missing_road_leg_does_not_expose_other_region(self) -> None:
        observed = state([])
        observed["navigation"]["road"] = {
            "status": "no_road", "target": None, "leg": None
        }

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertNotIn("explore_other_region", criteria)

    def test_day_and_dusk_torch_options_depend_on_equipment(self) -> None:
        for phase in ("day", "dusk"):
            for equipped in (False, True):
                with self.subTest(phase=phase, equipped=equipped):
                    observed = state([])
                    observed["world"]["phase"] = phase
                    slot = "equipped" if equipped else "items"
                    observed["player"]["inventory"][slot] = [
                        {"prefab": "torch", "count": 1}
                    ]
                    criteria, _ = jev_agent.build_candidates(observed)
                    self.assertNotIn("equip_torch", criteria)
                    self.assertEqual("unequip_torch" in criteria, equipped)
                    self.assertIn("wait", criteria)
                    self.assertIn("explore_current_region", criteria)

    def test_night_torch_options_depend_on_fire_and_equipment(self) -> None:
        for lit_fire, equipped in ((False, False), (True, True)):
            with self.subTest(lit_fire=lit_fire, equipped=equipped):
                observed = state([lit_campfire()] if lit_fire else [])
                observed["world"]["phase"] = "night"
                slot = "equipped" if equipped else "items"
                observed["player"]["inventory"][slot] = [
                    {"prefab": "torch", "count": 1}
                ]
                criteria, _ = jev_agent.build_candidates(observed)
                self.assertEqual("equip_torch" in criteria, not equipped)
                self.assertEqual("unequip_torch" in criteria, lit_fire and equipped)
                if lit_fire:
                    self.assertIn("wait", criteria)
                    self.assertIn("explore_current_region", criteria)

    def test_campfire_is_built_only_at_night_without_lit_fire(self) -> None:
        dusk = state([])
        dusk["world"]["phase"] = "dusk"
        dusk["crafting"]["campfire"] = True
        night = state([])
        night["world"]["phase"] = "night"
        night["crafting"]["campfire"] = True
        safe_night = state([lit_campfire()])
        safe_night["world"]["phase"] = "night"
        safe_night["crafting"]["campfire"] = True

        dusk_criteria, _ = jev_agent.build_candidates(dusk)
        night_criteria, _ = jev_agent.build_candidates(night)
        safe_criteria, _ = jev_agent.build_candidates(safe_night)

        self.assertNotIn("build_campfire", dusk_criteria)
        self.assertIn("build_campfire", night_criteria)
        self.assertNotIn("build_campfire", safe_criteria)

    def test_depleted_near_grass_does_not_hide_live_far_grass(self) -> None:
        observed = state([grass(10, 0.5, False), grass(20, 2.0, True)])

        criteria, dispatch = jev_agent.build_candidates(observed)

        self.assertIn("collect_grass", criteria)
        self.assertEqual(dispatch["collect_grass"]["guid"], 20)

    def test_no_grass_action_when_every_patch_is_depleted(self) -> None:
        criteria, _ = jev_agent.build_candidates(
            state([grass(10, 0.5, False), grass(20, 2.0, False)])
        )

        self.assertNotIn("collect_grass", criteria)

    def test_immediate_hostile_unarmed_exposes_only_flee(self) -> None:
        criteria, _ = jev_agent.build_candidates(state([grass(10, 2.0, True), hostile(30, 4.0)]))

        self.assertEqual(set(criteria), {"flee_from_nearest_hostile"})

    def test_low_hunger_cannot_remove_flee_during_attack(self) -> None:
        observed = state([hostile(30, 4.0)])
        observed["player"]["vitals"]["hunger"] = 10

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"flee_from_nearest_hostile"})

    def test_attackable_non_pursuer_does_not_offer_flee(self) -> None:
        observed = state([hostile(30, 4.0)])
        observed["nearby"][0]["activeThreat"] = False

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertNotIn("flee_from_nearest_hostile", criteria)

    def test_pursuer_can_be_fled_even_when_not_attackable(self) -> None:
        observed = state([hostile(30, 4.0)])
        observed["nearby"][0]["attackable"] = False

        criteria, dispatch = jev_agent.build_candidates(observed)

        self.assertIn("flee_from_nearest_hostile", criteria)
        self.assertEqual(dispatch["flee_from_nearest_hostile"]["guid"], 30)

    def test_pursuer_in_twelve_unit_escape_range_offers_flee(self) -> None:
        criteria, _ = jev_agent.build_candidates(state([hostile(30, 10.0)]))

        self.assertIn("flee_from_nearest_hostile", criteria)

    def test_carried_weapon_must_be_equipped_before_attack(self) -> None:
        observed = state([hostile(30, 4.0)])
        observed["player"]["inventory"]["items"] = [
            {"prefab": "spear", "count": 1, "weapon": True, "weapon_damage": 34}
        ]

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"flee_from_nearest_hostile", "equip_weapon"})

    def test_equipped_weapon_allows_attack_or_flee(self) -> None:
        observed = state([hostile(30, 4.0)])
        observed["player"]["inventory"]["equipped"] = [
            {"prefab": "spear", "count": 1, "weapon": True, "weapon_damage": 34}
        ]

        criteria, _ = jev_agent.build_candidates(observed)

        self.assertEqual(set(criteria), {"flee_from_nearest_hostile", "attack_nearest_hostile"})


class ControllerTests(unittest.TestCase):
    def test_craft_torch_unequips_auto_equipped_torch_before_night(self) -> None:
        for phase in ("day", "dusk"):
            with self.subTest(phase=phase):
                before = state([])
                before["world"]["phase"] = phase
                before["crafting"]["torch"] = True
                crafted = state([])
                crafted["world"]["phase"] = phase
                crafted["player"]["inventory"]["equipped"] = [
                    {"prefab": "torch", "count": 1}
                ]
                with (
                    patch.object(controller, "latest_state", return_value=(before, 100)),
                    patch.object(controller, "find_game_window", return_value=123),
                    patch.object(controller, "focus_game"),
                    patch.object(controller, "tap"),
                    patch.object(controller, "wait_for_state_condition", return_value=(crafted, 101)) as verify,
                    patch.object(controller, "unequip_torch") as unequip,
                ):
                    controller.craft_torch(Path("unused.log"))

                self.assertTrue(verify.call_args.args[2](crafted))
                unequip.assert_called_once_with(Path("unused.log"), daylight_only=True)

    def test_craft_torch_keeps_torch_when_night_or_not_auto_equipped(self) -> None:
        for phase, equipped in (("night", True), ("day", False)):
            with self.subTest(phase=phase, equipped=equipped):
                before = state([])
                before["crafting"]["torch"] = True
                crafted = state([])
                crafted["world"]["phase"] = phase
                slot = "equipped" if equipped else "items"
                crafted["player"]["inventory"][slot] = [
                    {"prefab": "torch", "count": 1}
                ]
                with (
                    patch.object(controller, "latest_state", return_value=(before, 100)),
                    patch.object(controller, "find_game_window", return_value=123),
                    patch.object(controller, "focus_game"),
                    patch.object(controller, "tap"),
                    patch.object(controller, "wait_for_state_condition", return_value=(crafted, 101)),
                    patch.object(controller, "unequip_torch") as unequip,
                ):
                    controller.craft_torch(Path("unused.log"))
                unequip.assert_not_called()

    def test_craft_cleanup_does_not_unequip_after_night_begins(self) -> None:
        night = state([])
        night["world"]["phase"] = "night"
        night["player"]["inventory"]["equipped"] = [
            {"prefab": "torch", "count": 1}
        ]
        with (
            patch.object(controller, "latest_state", return_value=(night, 101)),
            patch.object(controller, "tap") as tap,
        ):
            controller.unequip_torch(Path("unused.log"), daylight_only=True)
        tap.assert_not_called()

    def test_craft_cleanup_is_noop_if_torch_was_already_unequipped(self) -> None:
        daylight = state([])
        daylight["player"]["inventory"]["items"] = [
            {"prefab": "torch", "count": 1}
        ]
        with (
            patch.object(controller, "latest_state", return_value=(daylight, 101)),
            patch.object(controller, "tap") as tap,
        ):
            controller.unequip_torch(Path("unused.log"), daylight_only=True)
        tap.assert_not_called()

    def test_eat_cooks_raw_food_first_when_fire_is_available(self) -> None:
        raw = state([lit_campfire()])
        raw["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 1}]
        cooked = state([lit_campfire()])
        cooked["player"]["inventory"]["items"] = [{"prefab": "cookedmeat", "count": 1}]
        eaten = state([lit_campfire()])
        eaten["player"]["vitals"]["hunger"] = 150
        tap = Mock()
        with (
            patch.object(controller, "latest_state", side_effect=[(raw, 100), (cooked, 101)]),
            patch.object(controller, "cook_food") as cook,
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap", tap),
            patch.object(controller, "wait_for_state_condition", return_value=(eaten, 102)) as verified,
        ):
            controller.eat_safe_food(Path("unused.log"))

        cook.assert_called_once_with(Path("unused.log"))
        tap.assert_called_once_with([controller.VK["eat_safe_food"]], 0.08)
        self.assertTrue(verified.call_args.args[2](eaten))

    def test_eat_uses_existing_cooked_food_without_recooking(self) -> None:
        carried = state([lit_campfire()])
        carried["player"]["inventory"]["items"] = [
            {"prefab": "meat", "count": 1},
            {"prefab": "cookedmeat", "count": 1},
        ]
        eaten = state([lit_campfire()])
        eaten["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 1}]
        with (
            patch.object(controller, "latest_state", return_value=(carried, 100)),
            patch.object(controller, "cook_food") as cook,
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap"),
            patch.object(controller, "wait_for_state_condition", return_value=(eaten, 101)),
        ):
            controller.eat_safe_food(Path("unused.log"))

        cook.assert_not_called()

    def test_eat_falls_back_to_raw_berries_when_cooking_fails(self) -> None:
        raw = state([lit_campfire()])
        raw["player"]["inventory"]["items"] = [{"prefab": "berries", "count": 1}]
        eaten = state([lit_campfire()])
        eaten["player"]["vitals"]["hunger"] = 150
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(raw, 100)),
            patch.object(controller, "cook_food", side_effect=RuntimeError("cook failed")),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap", tap),
            patch.object(controller, "wait_for_state_condition", return_value=(eaten, 101)),
        ):
            controller.eat_safe_food(Path("unused.log"))

        tap.assert_called_once_with([controller.VK["eat_safe_food"]], 0.08)

    def test_eat_does_not_fall_back_to_unsafe_raw_meat(self) -> None:
        raw = state([lit_campfire()])
        raw["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 1}]
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(raw, 100)),
            patch.object(controller, "cook_food", side_effect=RuntimeError("cook failed")),
            patch.object(controller, "tap", tap),
        ):
            with self.assertRaisesRegex(RuntimeError, "cook failed"):
                controller.eat_safe_food(Path("unused.log"))

        tap.assert_not_called()

    def test_completed_work_collects_all_expected_drop_types(self) -> None:
        for target_tag, action, drop in (
            ("CHOP_workable", "chop_nearest_tree", "log"),
            ("MINE_workable", "mine_nearest_rock", "rocks"),
        ):
            with self.subTest(action=action):
                required_drops = (
                    {"log", "pinecone", "acorn"} if drop == "log"
                    else {"rocks", "flint", "nitre", "goldnugget"}
                )
                self.assertTrue(required_drops <= controller.WORK_DROP_PREFABS[target_tag])
                observed = state([{
                    "guid": 20, "prefab": "evergreen" if drop == "log" else "rock1",
                    "distance": 2.0, "dx": 2.0, "dz": 1.0,
                    "tags": [target_tag], "work_required": 6,
                }])
                tool = "axe" if drop == "log" else "pickaxe"
                observed["player"]["inventory"]["items"] = [
                    {"prefab": tool, "count": 1, "durability_percent": 1.0}
                ]
                finished = state([])
                finished["work"] = {
                    "sequence": 1,
                    "action": "chop" if drop == "log" else "mine",
                    "target_guid": 21,
                    "x": 3.0,
                    "z": 1.0,
                    "status": "completed",
                }
                with (
                    patch.object(controller, "latest_state", return_value=(observed, 100)),
                    patch.object(controller, "find_game_window", return_value=123),
                    patch.object(controller, "focus_game"),
                    patch.object(controller, "tap"),
                    patch.object(controller, "wait_for_state_condition", return_value=(finished, 101)),
                    patch.object(controller, "collect_nearby_drops") as gather,
                ):
                    controller.complete_work_action(Path("unused.log"), action, target_tag)

                gather.assert_called_once_with(
                    Path("unused.log"), controller.WORK_DROP_PREFABS[target_tag],
                    (3.0, 1.0), finished, 101, preexisting_drop_guids=set(),
                )

    def test_work_failed_status_does_not_collect_drops(self) -> None:
        observed = state([{
            "guid": 20, "prefab": "evergreen", "distance": 2.0,
            "tags": ["CHOP_workable"], "work_required": 6,
        }])
        observed["player"]["inventory"]["items"] = [
            {"prefab": "axe", "count": 1, "durability_percent": 1.0}
        ]
        failed = state([])
        failed["work"] = {
            "sequence": 1, "action": "chop", "target_guid": 20,
            "x": 2.0, "z": 0.0, "status": "failed", "reason": "tool_broke",
        }
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap"),
            patch.object(controller, "wait_for_state_condition", return_value=(failed, 101)),
            patch.object(controller, "collect_nearby_drops") as gather,
        ):
            with self.assertRaisesRegex(RuntimeError, "tool_broke"):
                controller.complete_work_action(Path("unused.log"), "chop_nearest_tree", "CHOP_workable")

        gather.assert_not_called()

    def test_work_waits_for_mod_acknowledgement_and_completion(self) -> None:
        observed = state([{
            "guid": 20, "prefab": "evergreen", "distance": 2.0,
            "tags": ["CHOP_workable"], "work_required": 6,
        }])
        observed["work"] = {"sequence": 4, "status": "completed"}
        observed["player"]["inventory"]["items"] = [
            {"prefab": "axe", "count": 1, "durability_percent": 1.0}
        ]
        started = state([])
        started["work"] = {
            "sequence": 5, "action": "chop", "target_guid": 21,
            "x": 3.0, "z": 1.0, "status": "started",
        }
        finished = json.loads(json.dumps(started))
        finished["work"]["status"] = "completed"
        snapshots = iter([(started, 101), (finished, 102)])

        def next_work_state(_path, _position, predicate, **_kwargs):
            fresh, position = next(snapshots)
            self.assertTrue(predicate(fresh))
            return fresh, position

        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap"),
            patch.object(controller, "wait_for_state_condition", side_effect=next_work_state) as verify,
            patch.object(controller, "collect_nearby_drops") as gather,
        ):
            controller.complete_work_action(
                Path("unused.log"), "chop_nearest_tree", "CHOP_workable",
            )

        self.assertEqual(verify.call_count, 2)
        gather.assert_called_once_with(
            Path("unused.log"), controller.WORK_DROP_PREFABS["CHOP_workable"],
            (3.0, 1.0), finished, 102, preexisting_drop_guids=set(),
        )

    def test_work_drop_collection_stays_near_completed_target(self) -> None:
        observed = state([
            {"guid": 31, "prefab": "log", "distance": 2.0, "dx": 2.0, "dz": 0.0,
             "tags": ["_inventoryitem"]},
            {"guid": 32, "prefab": "log", "distance": 8.0, "dx": 8.0, "dz": 0.0,
             "tags": ["_inventoryitem"]},
        ])
        empty = state([])
        with (
            patch.object(controller, "collect", return_value=True) as pickup,
            patch.object(controller, "latest_state", return_value=(empty, 102)),
            patch.object(controller, "wait_for_fresh_state", return_value=(empty, 103)),
        ):
            count = controller.collect_nearby_drops(
                Path("unused.log"), controller.WORK_DROP_PREFABS["CHOP_workable"],
                (2.0, 0.0), observed, 101,
            )

        self.assertEqual(count, 1)
        self.assertEqual(pickup.call_args.kwargs["preferred_guid"], 31)
        self.assertTrue(pickup.call_args.kwargs["strict_preferred"])

    def test_work_drop_collection_includes_mixed_products_and_new_unknown_drop(self) -> None:
        mixed = state([
            {"guid": 31, "prefab": "rocks", "distance": 2, "dx": 2, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 32, "prefab": "flint", "distance": 3, "dx": 3, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 33, "prefab": "nitre", "distance": 3.5, "dx": 3.5, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 34, "prefab": "unknown_mine_drop", "distance": 4, "dx": 4, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 35, "prefab": "berries", "distance": 4, "dx": 4, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 36, "prefab": "goldnugget", "distance": 8, "dx": 8, "dz": 0,
             "tags": ["_inventoryitem"]},
        ])
        snapshots = []
        for count in range(4):
            snapshots.append((state(mixed["nearby"][count + 1:]), 102 + count))
        snapshots.append((state([]), 106))
        with (
            patch.object(controller, "collect", return_value=True) as pickup,
            patch.object(controller, "latest_state", side_effect=snapshots),
            patch.object(controller, "wait_for_fresh_state", return_value=(state([]), 107)),
        ):
            count = controller.collect_nearby_drops(
                Path("unused.log"), controller.WORK_DROP_PREFABS["MINE_workable"],
                (2.0, 0.0), mixed, 101, preexisting_drop_guids={35},
            )

        self.assertEqual(count, 4)
        self.assertEqual([call.kwargs["preferred_guid"] for call in pickup.call_args_list],
                         [31, 32, 33, 34])

    def test_failed_drop_pickup_does_not_hide_other_work_products(self) -> None:
        drops = state([
            {"guid": 41, "prefab": "log", "distance": 1, "dx": 1, "dz": 0,
             "tags": ["_inventoryitem"]},
            {"guid": 42, "prefab": "pinecone", "distance": 2, "dx": 2, "dz": 0,
             "tags": ["_inventoryitem"]},
        ])
        with (
            patch.object(controller, "collect", side_effect=[False, True]) as pickup,
            patch.object(controller, "latest_state", side_effect=[
                (drops, 102), (state([]), 103),
            ]),
            patch.object(controller, "wait_for_fresh_state", return_value=(state([]), 104)),
        ):
            count = controller.collect_nearby_drops(
                Path("unused.log"), controller.WORK_DROP_PREFABS["CHOP_workable"],
                (0.0, 0.0), drops, 101,
            )
        self.assertEqual(count, 1)
        self.assertEqual([call.kwargs["preferred_guid"] for call in pickup.call_args_list],
                         [41, 42])

    def test_follow_road_combines_successive_waypoints_within_distance_budget(self) -> None:
        def road_state(position: float, waypoint: float) -> dict:
            observed = state([])
            observed["player"]["position"]["x"] = position
            observed["navigation"]["road"] = {
                "status": "ready",
                "target": {"x": waypoint, "z": 0.0},
                "leg": {"x": waypoint, "z": 0.0, "distance": waypoint - position},
            }
            return observed

        first = road_state(0.0, 2.0)
        second = road_state(2.0, 4.0)
        third = road_state(4.0, 6.0)
        with (
            patch.object(controller, "latest_state", side_effect=[
                (first, 100), (first, 100), (second, 101),
                (second, 101), (third, 102), (third, 102),
            ]),
            patch.object(controller, "explore_leg", return_value=True) as leg,
        ):
            moved = controller.follow_road(
                Path("unused.log"), 2.0, 0.0, 2.0, max_distance=4.0,
            )

        self.assertTrue(moved)
        self.assertEqual(leg.call_count, 2)
        self.assertEqual(leg.call_args_list[0].kwargs["frontier_x"], 2.0)
        self.assertEqual(leg.call_args_list[1].kwargs["frontier_x"], 4.0)

    def test_follow_road_stops_for_new_close_resource(self) -> None:
        first = state([])
        first["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 2.0, "z": 0.0},
            "leg": {"x": 2.0, "z": 0.0, "distance": 2.0},
        }
        second = json.loads(json.dumps(first))
        second["player"]["position"]["x"] = 2.0
        second["nearby"] = [grass(10, 2.0, True)]
        second["navigation"]["road"]["target"]["x"] = 4.0
        second["navigation"]["road"]["leg"]["x"] = 4.0
        third = json.loads(json.dumps(second))
        third["player"]["position"]["x"] = 4.0
        with (
            patch.object(controller, "latest_state", side_effect=[
                (first, 100), (first, 100), (second, 101),
                (second, 101), (third, 102),
            ]),
            patch.object(controller, "explore_leg", return_value=True) as leg,
        ):
            moved = controller.follow_road(Path("unused.log"), 2.0, 0.0, 2.0)

        self.assertTrue(moved)
        self.assertEqual(leg.call_count, 2)

    def test_follow_road_stops_at_new_biome_not_new_topology(self) -> None:
        def road_state(position: float, topology: str, biome: str) -> dict:
            observed = state([])
            observed["player"]["position"]["x"] = position
            observed["navigation"].update({
                "current_region": topology,
                "current_biome": biome,
                "road": {
                    "status": "ready",
                    "target": {"x": position + 2.0, "z": 0.0},
                    "leg": {"x": position + 2.0, "z": 0.0, "distance": 2.0},
                },
            })
            return observed

        first = road_state(0.0, "A", "forest")
        same_biome = road_state(2.0, "B", "forest")
        new_biome = road_state(4.0, "B", "savanna")
        with (
            patch.object(controller, "latest_state", side_effect=[
                (first, 100), (first, 100), (same_biome, 101),
                (same_biome, 101), (new_biome, 102), (new_biome, 102),
            ]),
            patch.object(controller, "explore_leg", return_value=True) as leg,
        ):
            moved = controller.follow_road(Path("unused.log"), 2.0, 0.0, 2.0)

        self.assertTrue(moved)
        self.assertEqual(leg.call_count, 2)

    def test_exploration_prompt_uses_biome(self) -> None:
        observed = state([])
        observed["navigation"]["current_biome"] = "rocky"
        observed["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 2.0, "z": 0.0, "distance": 2.0},
            "leg": {"x": 2.0, "z": 0.0, "distance": 2.0},
        }
        criteria, _ = jev_agent.build_candidates(observed)
        self.assertIn("rocky", criteria["explore_current_region"])
        self.assertIn("rocky", criteria["explore_other_region"])

    def test_collection_stops_when_night_falls_even_beside_campfire(self) -> None:
        before = state([lit_campfire(), grass(10, 4.0, True)])
        after = state([lit_campfire(), grass(10, 2.0, True)])
        after["world"]["phase"] = "night"
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "wait_for_fresh_state", side_effect=[
                (before, 101), (after, 102),
            ]),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            completed = controller.collect(Path("unused.log"), "grass", preferred_guid=10)

        self.assertFalse(completed)
        self.assertEqual(tap.call_count, 1)

    def test_explore_rechecks_night_light_before_moving(self) -> None:
        observed = state([lit_campfire()])
        observed["world"]["phase"] = "night"
        tap = Mock()
        with patch.object(controller, "latest_state", return_value=(observed, 100)), patch.object(controller, "tap", tap):
            moved = controller.explore_leg(Path("unused.log"), 0.0, 4.0, 4.0)

        self.assertFalse(moved)
        tap.assert_not_called()

    def test_road_exploration_stops_when_night_falls_without_torch(self) -> None:
        before = state([lit_campfire()])
        before["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 4.0, "z": 0.0},
            "leg": {"x": 4.0, "z": 0.0, "distance": 4.0},
        }
        after = json.loads(json.dumps(before))
        after["world"]["phase"] = "night"
        after["player"]["position"]["x"] = 1.0
        tap = Mock()
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (before, 105)]),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", return_value=(after, 106)),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            controller.explore_leg(
                Path("unused.log"), 4.0, 0.0, 4.0,
                frontier_x=4.0, frontier_z=0.0, navigation_mode="road",
            )

        self.assertEqual(tap.call_count, 1)

    def test_cook_food_verifies_cooked_inventory_gain(self) -> None:
        before = state([lit_campfire()])
        before["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 2}]
        after = state([lit_campfire()])
        after["player"]["inventory"]["items"] = [
            {"prefab": "meat", "count": 1}, {"prefab": "cookedmeat", "count": 1}
        ]
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "tap", tap),
            patch.object(controller, "wait_for_state_condition", return_value=(after, 101)) as verified,
        ):
            controller.cook_food(Path("unused.log"))

        tap.assert_called_once_with([controller.VK["cook_food"]], 0.08)
        self.assertTrue(verified.call_args.args[2](after))

    def test_cook_food_rejects_stale_missing_fire(self) -> None:
        observed = state([])
        observed["player"]["inventory"]["items"] = [{"prefab": "meat", "count": 1}]
        with patch.object(controller, "latest_state", return_value=(observed, 100)):
            with self.assertRaisesRegex(RuntimeError, "No nearby lit cooker"):
                controller.cook_food(Path("unused.log"))

    def test_road_leg_brakes_and_waits_for_post_release_telemetry(self) -> None:
        before = state([])
        before["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 4.0, "z": 0.0},
            "leg": {"x": 4.0, "z": 0.0, "distance": 4.0},
        }
        halfway = json.loads(json.dumps(before))
        halfway["player"]["position"]["x"] = 2.0
        near = json.loads(json.dumps(before))
        near["player"]["position"]["x"] = 3.5
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        tap = Mock()
        with (
            patch.object(controller, "latest_state", side_effect=[
                (before, 100), (before, 105), (halfway, 106),
            ]),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", side_effect=[
                (halfway, 106), (near, 107),
            ]) as fresh_wait,
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            controller.explore_leg(
                Path("unused.log"), 4.0, 0.0, 4.0,
                frontier_x=4.0, frontier_z=0.0, navigation_mode="road",
            )

        self.assertEqual(tap.call_count, 2)
        self.assertTrue(all(args.args[1] <= 0.5 for args in tap.call_args_list))
        self.assertEqual(fresh_wait.call_args_list[0].args[1], 105)
        self.assertEqual(fresh_wait.call_args_list[1].args[1], 106)

    def test_road_exploration_uses_road_leg_and_rejects_stalled_waypoint(self) -> None:
        observed = state([])
        observed["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 4.0, "z": 0.0},
            "leg": {"x": 4.0, "z": 0.0, "distance": 4.0},
        }
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", return_value=(observed, 101)),
            patch.object(controller, "wait_for_state_condition", return_value=(observed, 103)),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            for _ in range(2):
                controller.explore_leg(
                    Path("unused.log"), 4.0, 0.0, 4.0,
                    frontier_x=4.0, frontier_z=0.0, navigation_mode="road",
                )

        self.assertEqual(tap.call_args_list[-1], call([controller.VK["reject_road"]], 0.08))

    def test_explore_walks_one_frontier_leg(self) -> None:
        before = state([])
        after = state([])
        after["player"]["position"]["z"] = 3.5
        tap = Mock()
        release = Mock()
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)

        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", return_value=(after, 101)),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys", release),
        ):
            controller.explore_leg(
                Path("unused.log"),
                target_x=0.0,
                target_z=4.0,
                leg_distance=4.0,
                full_leg_seconds=1.0,
            )

        tap.assert_called_once_with([controller.VK["up"]], 1.0)
        release.assert_called_once_with()

    def test_region_leg_within_arrival_tolerance_does_not_micro_correct(self) -> None:
        observed = state([])
        observed["navigation"]["leg"] = {
            "x": 0.0, "z": 1.2, "distance": 1.2,
        }
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "tap", tap),
        ):
            moved = controller.explore_leg(Path("unused.log"), 0.0, 1.2, 1.2)

        self.assertFalse(moved)
        tap.assert_not_called()

    def test_road_leg_does_not_reverse_after_small_overshoot(self) -> None:
        before = state([])
        before["navigation"]["road"] = {
            "status": "ready",
            "target": {"x": 2.0, "z": 0.0},
            "leg": {"x": 2.0, "z": 0.0, "distance": 2.0},
        }
        after = json.loads(json.dumps(before))
        after["player"]["position"]["x"] = 2.9
        tap = Mock()
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (before, 105)]),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", return_value=(after, 106)),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            controller.explore_leg(
                Path("unused.log"), 2.0, 0.0, 2.0,
                frontier_x=2.0, frontier_z=0.0, navigation_mode="road",
            )

        self.assertEqual(tap.call_count, 1)
        self.assertEqual(controller.ROAD_ARRIVAL_DISTANCE, 1.25)

    def test_two_stalled_exploration_legs_reject_current_frontier(self) -> None:
        observed = state([])
        replanned = state([])
        replanned["navigation"]["target"]["x"] = 12.0
        controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", side_effect=[(observed, 101), (observed, 102)]),
            patch.object(controller, "wait_for_state_condition", return_value=(replanned, 103)) as verified,
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            for _ in range(2):
                controller.explore_leg(
                    Path("unused.log"), 0.0, 4.0, 4.0, 1.0,
                    frontier_x=0.0, frontier_z=12.0,
                )

        self.assertEqual(tap.call_args_list, [
            call([controller.VK["up"]], 1.0),
            call([controller.VK["up"]], 1.0),
            call([controller.VK["reject_frontier"]], 0.08),
        ])
        verified.assert_called_once()
        self.assertEqual(controller.EXPLORATION_STALL["count"], 0)

    def test_explore_uses_updated_leg_for_same_frontier(self) -> None:
        observed = state([])
        observed["navigation"]["leg"]["x"] = 4.0
        observed["navigation"]["leg"]["z"] = 0.0
        after = state([])
        after["player"]["position"]["x"] = 3.5
        tap = Mock()
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(controller, "wait_for_fresh_state", return_value=(after, 101)),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            controller.explore_leg(Path("unused.log"), 0.0, 4.0, 4.0)

        tap.assert_called_once_with([controller.VK["right"]], 1.0)

    def test_approach_uses_longer_pulses_for_distant_targets(self) -> None:
        self.assertEqual(controller.approach_step_duration(8.0), 1.0)
        self.assertGreater(controller.approach_step_duration(4.0), 0.70)
        self.assertEqual(controller.approach_step_duration(1.1), 0.18)

    def test_torch_is_not_treated_as_a_combat_weapon(self) -> None:
        self.assertFalse(controller.is_weapon_item({"prefab": "torch", "weapon": True}))

    def test_flee_moves_directly_away_from_hostile(self) -> None:
        observed = state([hostile(30, 3.0)])
        safe = state([])
        tap = Mock()
        release = Mock()

        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(
                controller,
                "wait_for_fresh_state",
                side_effect=[(safe, 101), (safe, 102)],
            ),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys", release),
        ):
            controller.flee_from_hostile(Path("unused.log"), preferred_guid=30)

        tap.assert_called_once_with([controller.VK["left"]], 1.0)
        release.assert_called_once_with()

    def test_flee_repeats_until_chasing_hostile_is_clear(self) -> None:
        observed = state([hostile(30, 3.0)])
        chasing_1 = state([hostile(30, 5.0)])
        chasing_2 = state([hostile(30, 9.0)])
        safe = state([])
        tap = Mock()

        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(
                controller,
                "wait_for_fresh_state",
                side_effect=[
                    (chasing_1, 101),
                    (chasing_2, 102),
                    (safe, 103),
                    (safe, 104),
                ],
            ),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys"),
        ):
            controller.flee_from_hostile(Path("unused.log"), preferred_guid=30)

        self.assertEqual(tap.call_count, 3)
        tap.assert_has_calls(
            [call([controller.VK["left"]], 1.0)] * 3
        )

    def test_depleted_preferred_guid_falls_back_to_live_grass(self) -> None:
        observed = state([grass(10, 0.4, False), grass(20, 0.8, True)])

        selected = controller.find_collectible_target(
            observed,
            "grass",
            preferred_guid=10,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["guid"], 20)

    def test_inventory_gain_finishes_collect_while_plant_remains(self) -> None:
        before = state([grass(20, 0.8, True)], cutgrass=0)
        after = state([grass(20, 0.8, True)], cutgrass=1)
        tap = Mock()
        release = Mock()

        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"),
            patch.object(
                controller,
                "wait_for_fresh_state",
                side_effect=[(before, 101), (after, 102)],
            ),
            patch.object(controller, "tap", tap),
            patch.object(controller, "release_movement_keys", release),
        ):
            controller.collect(Path("unused.log"), "grass", preferred_guid=20)

        tap.assert_called_once_with([controller.VK["interact"]], 0.08)
        release.assert_called_once_with()


class SmokeTests(unittest.TestCase):
    def test_controller_self_test(self) -> None:
        self.assertEqual(controller.run_self_test(), 0)


class ThresholdTests(unittest.TestCase):
    def test_execution_threshold_policy_covers_all_other_actions(self) -> None:
        for action in (
            "collect_grass", "cook_food", "craft_torch", "equip_torch",
            "unequip_torch", "craft_axe", "craft_pickaxe", "chop_nearest_tree",
            "mine_nearest_rock", "build_campfire", "build_sciencemachine", "equip_weapon",
        ):
            with self.subTest(action=action):
                self.assertEqual(jev_agent.default_execution_threshold(action), 0.25)
        for action, expected in (
            ("flee_from_nearest_hostile", 0.0), ("wait", 0.0),
            ("explore_current_region", 0.15), ("explore_other_region", 0.15),
            ("attack_nearest_hostile", 0.45),
        ):
            with self.subTest(action=action):
                self.assertEqual(jev_agent.default_execution_threshold(action), expected)

        observed = state([])
        observed["player"]["inventory"]["items"] = [{"prefab": "carrot", "count": 1}]
        for hunger, expected in ((100, 0.25), (3, 0.0)):
            with self.subTest(action="eat_safe_food", hunger=hunger):
                observed["player"]["vitals"]["hunger"] = hunger
                self.assertIn("eat_safe_food", jev_agent.build_candidates(observed)[0])
                self.assertEqual(
                    jev_agent.default_execution_threshold("eat_safe_food", observed), expected
                )


class TelemetryTests(unittest.TestCase):
    def test_truncated_line_is_ignored_and_chunked_frame_waits_until_complete(self) -> None:
        original = {"schema": 6, "world": {"day": 1}}
        updated = {"schema": 7, "world": {"day": 2}, "blob": "x" * 7000}
        first_line = f"[JEV_DST_STATE]{json.dumps(original)}\n".encode("utf-8")
        truncated = ("[JEV_DST_STATE]" + json.dumps(updated)[:4070] + "\n").encode("utf-8")
        encoded = json.dumps(updated)
        parts = [encoded[index:index + 3000] for index in range(0, len(encoded), 3000)]
        chunk_lines = [
            f"[JEV_DST_CHUNK]test-1:{index}:{len(parts)}:{part}\t\n".encode("utf-8")
            for index, part in enumerate(parts, 1)
        ]

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "client_log.txt"
            log_path.write_bytes(first_line + truncated + b"".join(chunk_lines[:-1]))
            state_before, position_before = telemetry.latest_state(log_path)
            self.assertEqual(state_before, original)
            self.assertEqual(position_before, len(first_line))

            with log_path.open("ab") as stream:
                stream.write(chunk_lines[-1])
            state_after, position_after = telemetry.latest_state(log_path)
            self.assertEqual(state_after, updated)
            self.assertEqual(position_after, log_path.stat().st_size)

    def test_incomplete_last_log_line_is_not_accepted(self) -> None:
        complete = b'[JEV_DST_STATE]{"schema":6}\n'
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "client_log.txt"
            log_path.write_bytes(complete + b'[JEV_DST_STATE]{"schema":7}')

            parsed, position = telemetry.latest_state(log_path)

            self.assertEqual(parsed["schema"], 6)
            self.assertEqual(position, len(complete))


if __name__ == "__main__":
    unittest.main()
