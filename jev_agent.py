"""Use JEV to choose one bounded DST action from fresh mod telemetry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

import controller
from daily_planner import DailyPlanner


DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_INSTALLED_ENV = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods\jev_dst_agent\.env"
)
SAFE_COLLECT_PREFABS = {
    "grass": "Cut grass source; needed urgently for a torch.",
    "sapling": "Twig source; needed urgently for a torch.",
    "sapling_moon": "Twig source; needed for basic crafting.",
    "twigs": "Loose twigs; needed urgently for a torch.",
    "cutgrass": "Loose cut grass; needed urgently for a torch.",
    "flint": "Loose flint; useful for early tools.",
    "log": "Loose log; needed for campfires and early structures.",
    "rocks": "Loose rocks; needed for early tools and structures.",
    "goldnugget": "Loose gold; useful for early tools and structures.",
    "nitre": "Loose nitre from mined rocks; useful for later crafting.",
    "pinecone": "Loose pine cone from a felled tree; can be replanted.",
    "acorn": "Loose birchnut from a felled tree; can be replanted.",
    "charcoal": "Loose charcoal from a burnt tree; useful for crafting.",
    "marble": "Loose marble from mining; useful for later crafting.",
    "moonrocknugget": "Loose moon rock from mining.",
    "berries": "Food that can reduce early hunger risk.",
    "berrybush": "Berry source if it currently offers a pick action.",
    "berrybush2": "Berry source if it currently offers a pick action.",
    "juicyberrybush": "Berry source if it currently offers a pick action.",
    "carrot": "Loose food.",
    "carrot_planted": "Food source.",
    "seeds": "Low-priority emergency food.",
}
SAFE_FOOD_PREFABS = {
    "cookedmeat",
    "cookedsmallmeat",
    "berries_cooked",
    "carrot_cooked",
    "berries",
    "carrot",
    "seeds_cooked",
    "seeds",
}
COOKABLE_FOOD_PRODUCTS = {
    "meat": "cookedmeat",
    "smallmeat": "cookedsmallmeat",
    "berries": "berries_cooked",
    "carrot": "carrot_cooked",
}
CRITICAL_HUNGER_RATIO = 0.30
TOOL_MAX_USES = {"axe": 100, "pickaxe": 33}
PICKABLE_SOURCE_PREFABS = {
    "grass",
    "sapling",
    "sapling_moon",
    "berrybush",
    "berrybush2",
    "juicyberrybush",
}


def load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE dotenv file without logging secret values."""
    if not path.exists():
        raise RuntimeError(f".env file was not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def resolve_env_path(explicit: Optional[Path]) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.extend(
        [
            Path(__file__).resolve().parent / ".env",
            Path.cwd() / ".env",
            DEFAULT_INSTALLED_ENV,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "No .env file found. Put it beside jev_agent.py or pass --env PATH."
    )


def inventory_counts(state: dict) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    inventory = state.get("player", {}).get("inventory", {})
    for item in inventory.get("items", []):
        prefab = str(item.get("prefab", "unknown"))
        counts[prefab] = counts.get(prefab, 0) + int(item.get("count", 1))
    for item in inventory.get("equipped", []):
        prefab = str(item.get("prefab", "unknown"))
        counts[prefab] = counts.get(prefab, 0) + int(item.get("count", 1))
    return counts


def is_weapon_item(item: dict) -> bool:
    return controller.is_weapon_item(item)


def weapon_status(state: dict) -> Tuple[bool, bool, Optional[str]]:
    inventory = state.get("player", {}).get("inventory", {})
    equipped = [item for item in inventory.get("equipped", []) if is_weapon_item(item)]
    carried = [item for item in inventory.get("items", []) if is_weapon_item(item)]
    best = max(
        equipped + carried,
        key=lambda item: float(item.get("weapon_damage") or 0),
        default=None,
    )
    return bool(equipped), bool(equipped or carried), None if best is None else best.get("prefab")


def has_equipped_torch(state: dict) -> bool:
    equipped = state.get("player", {}).get("inventory", {}).get("equipped", [])
    return any(item.get("prefab") == "torch" for item in equipped)


def has_nearby_lit_fire(state: dict, maximum_distance: float = 8.0) -> bool:
    return any(
        entity.get("prefab") in {"campfire", "firepit"}
        and "fire" in entity.get("tags", [])
        and float(entity.get("distance", 999)) <= maximum_distance
        for entity in state.get("nearby", [])
    )


def nearest_cookable_fire(state: dict, maximum_distance: float = 6.0) -> Optional[dict]:
    return min(
        (
            entity for entity in state.get("nearby", [])
            if entity.get("prefab") in {"campfire", "firepit"}
            and {"fire", "cooker"}.issubset(set(entity.get("tags", [])))
            and float(entity.get("distance", 999)) <= maximum_distance
        ),
        key=lambda entity: float(entity.get("distance", 999)),
        default=None,
    )


def conservative_tool_uses(state: dict, prefab: str) -> int:
    """Return the minimum possible uses represented by DST's rounded percent."""
    maximum = TOOL_MAX_USES[prefab]
    possible_uses = []
    inventory = state.get("player", {}).get("inventory", {})
    for item in inventory.get("items", []) + inventory.get("equipped", []):
        if item.get("prefab") != prefab or item.get("durability_percent") is None:
            continue
        reported = int(round(float(item["durability_percent"]) * 100))
        matching = [
            uses for uses in range(maximum + 1)
            if int(uses / maximum * 100 + 0.5) == reported
        ]
        if matching:
            possible_uses.append(min(matching))
    return max(possible_uses, default=0)


def has_immediate_threat(state: dict, threshold: float = 5.0) -> bool:
    return any(
        entity.get("activeThreat")
        and float(entity.get("distance", 999)) <= threshold
        for entity in state.get("nearby", [])
    )


def can_eat_safe_food(state: dict) -> bool:
    counts = inventory_counts(state)
    if any(counts.get(prefab, 0) > 0 for prefab in SAFE_FOOD_PREFABS):
        return True
    return nearest_cookable_fire(state) is not None and any(
        counts.get(prefab, 0) > 0 for prefab in COOKABLE_FOOD_PRODUCTS
    )


def action_allowed(state: dict, action_kind: str) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked). Filter actions that are meaningless or unsafe."""
    phase = state.get("world", {}).get("phase")
    torch_equipped = has_equipped_torch(state)
    nearby_lit_fire = has_nearby_lit_fire(state)
    immediate_threat = has_immediate_threat(state)
    if immediate_threat:
        allowed = {
            "flee_from_nearest_hostile",
            "equip_weapon",
            "attack_nearest_hostile",
        }
        if action_kind not in allowed:
            return False, "immediate threat: only flee, equip weapon, or attack are allowed"

    vitals = state.get("player", {}).get("vitals", {})
    hunger = float(vitals.get("hunger") or 0)
    hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
    if (
        not immediate_threat
        and hunger / hunger_max < CRITICAL_HUNGER_RATIO
        and can_eat_safe_food(state)
    ):
        # Eating dominates optional actions, but escape and immediate light remain available.
        emergency_actions = {"eat_safe_food", "flee_from_nearest_hostile"}
        if phase == "night" and not torch_equipped and not nearby_lit_fire:
            emergency_actions.update({"equip_torch", "craft_torch", "build_campfire"})
        if action_kind not in emergency_actions:
            return False, "critical hunger: eat carried food before optional actions"

    # night policy: no chopping or mining
    if phase == "night" and action_kind in {
        "chop_nearest_tree", "mine_nearest_rock", "equip_weapon", "attack_nearest_hostile"
    }:
        return False, "chopping, mining, equipping weapons, and attacking are forbidden at night"

    # A stationary campfire is not portable light for travel to a target.
    if phase == "night" and action_kind in {"explore", "collect"} and not torch_equipped:
        return False, "exploration or collection at night requires an equipped torch, even beside a lit fire"

    # night no torch: no movement or collection
    if phase == "night" and not torch_equipped and not nearby_lit_fire:
        allowed = {
            "wait",
            "craft_torch",
            "equip_torch",
            "build_campfire",
            "flee_from_nearest_hostile",
            "eat_safe_food",
        }
        if action_kind not in allowed:
            return False, "movement or collection blocked at night without light"

    # day/dusk or nearby fire: no torch equip
    if action_kind in {"equip_torch", "build_campfire"} and (phase != "night" or nearby_lit_fire):
        return False, "torch should not be equipped in daylight or beside a lit fire"

    # night equip torch: no unequip torch
    if action_kind == "unequip_torch" and phase == "night" and not nearby_lit_fire:
        return False, "torch should not be unequipped at night without a lit fire"

    return True, ""

def build_candidates(state: dict) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """Return [criteria, dispatch]. Generate available actions. """
    criteria: Dict[str, str] = {}
    dispatch: Dict[str, dict] = {}
    nearest_by_prefab: Dict[str, dict] = {}

    for entity in state.get("nearby", []):
        prefab = str(entity.get("prefab", ""))
        if prefab not in SAFE_COLLECT_PREFABS:
            continue
        tags = set(entity.get("tags", []))
        if prefab in PICKABLE_SOURCE_PREFABS:
            if entity.get("pickable") is not True:
                continue
        elif "pickable" not in tags and "_inventoryitem" not in tags:
            continue
        current = nearest_by_prefab.get(prefab)
        if current is None or float(entity.get("distance", 999)) < float(current.get("distance", 999)):
            nearest_by_prefab[prefab] = entity

    for prefab, entity in sorted(nearest_by_prefab.items()):
        action_id = f"collect_{prefab}"
        distance = float(entity.get("distance", 0))
        leftover_note = (
            " Chopping or mining already picks up nearby matching drops; use this for leftovers."
            if prefab in {"log", "rocks"} else ""
        )
        criteria[action_id] = (
            f"Approach and collect the nearest observed {prefab}, {distance:.2f} world units away. "
            f"{SAFE_COLLECT_PREFABS[prefab]} Choose only when its benefit exceeds exploration risk."
            f"{leftover_note}"
        )
        dispatch[action_id] = {
            "kind": "collect",
            "prefab": prefab,
            "guid": entity.get("guid"),
        }

    navigation = state.get("navigation", {})
    biome = navigation.get("current_biome") or "unidentified terrain"
    topology = navigation.get("current_region") or "unknown"
    frontier_target = navigation.get("target")
    frontier_leg = navigation.get("leg")
    if navigation.get("status") == "ready" and frontier_target and frontier_leg:
        criteria["explore_current_region"] = (
            f"Survey the unexplored edge of the current terrain biome ({biome}); already here. "
            f"The local frontier target is at "
            f"({float(frontier_target.get('x', 0)):.2f}, {float(frontier_target.get('z', 0)):.2f}), "
            f"{float(frontier_target.get('distance', 0)):.2f} units away. Walk only the next "
            f"{float(frontier_leg.get('distance', 0)):.2f}-unit leg, then stop and reconsider all actions. "
            f"This explores the present terrain type rather than following a road outward."
        )
        dispatch["explore_current_region"] = {
            "kind": "explore",
            "navigation_mode": "region",
            "target_x": frontier_leg.get("x"),
            "target_z": frontier_leg.get("z"),
            "leg_distance": frontier_leg.get("distance"),
            "frontier_x": frontier_target.get("x"),
            "frontier_z": frontier_target.get("z"),
        }
    road = navigation.get("road") or {}
    road_target = road.get("target") or {}
    road_leg = road.get("leg") or {}
    if road.get("status") == "ready" and road_target and road_leg:
        criteria["explore_other_region"] = (
            f"Follow an observed road from {biome} terrain (topology {topology}) to look for another biome. "
            f"The next road waypoint is {float(road_target.get('distance', 0)):.2f} units away; "
            f"begin with its {float(road_leg.get('distance', 0)):.2f}-unit leg, then follow live "
            "road waypoints for at most 10 units before reconsidering all actions. Stop sooner "
            "for a new terrain biome, a close new resource, a threat, a phase change, or darkness. "
            "A road may end without finding a new biome."
        )
        dispatch["explore_other_region"] = {
            "kind": "explore",
            "navigation_mode": "road",
            "target_x": road_leg.get("x"),
            "target_z": road_leg.get("z"),
            "leg_distance": road_leg.get("distance"),
            "frontier_x": road_target.get("x"),
            "frontier_z": road_target.get("z"),
        }

    counts = inventory_counts(state)
    torch_count = counts.get("torch", 0)
    torch_equipped = has_equipped_torch(state)
    if state.get("crafting", {}).get("torch", False) and torch_count == 0:
        criteria["craft_torch"] = (
            "Craft one torch now. This option is exposed only because the game reports that the "
            "recipe is currently craftable. If DST auto-equips it during day or dusk, the "
            "controller immediately unequips it to save fuel. Strongly prefer before night, "
            "especially during dusk."
        )
        dispatch["craft_torch"] = {"kind": "craft_torch"}
    if torch_count > 0 and not torch_equipped:
        criteria["equip_torch"] = (
            "Equip the existing torch because it is night and no lit campfire or firepit is nearby."
        )
        dispatch["equip_torch"] = {"kind": "equip_torch"}
    if torch_equipped:
        criteria["unequip_torch"] = (
            "Unequip the torch from the hand slot and return it to the backpack to preserve fuel. "
            "Useful during day or dusk, or when remaining beside a lit fire at night. "
            "If travelling away from fire at night, keep the torch equipped."
        )
        dispatch["unequip_torch"] = {"kind": "unequip_torch"}

    crafting = state.get("crafting", {})
    if counts.get("axe", 0) == 0 and crafting.get("axe", False):
        criteria["craft_axe"] = (
            "Craft a basic axe. Exposed only because the recipe is currently craftable and no axe is held. "
            "Choose when nearby trees or a need for logs justify spending materials."
        )
        dispatch["craft_axe"] = {"kind": "craft_axe"}
    if counts.get("pickaxe", 0) == 0 and crafting.get("pickaxe", False):
        criteria["craft_pickaxe"] = (
            "Craft a basic pickaxe. Exposed only because the recipe is currently craftable and no pickaxe is held. "
            "Choose when nearby mineable rocks justify spending materials."
        )
        dispatch["craft_pickaxe"] = {"kind": "craft_pickaxe"}

    nearby = state.get("nearby", [])
    chop_targets = [
        entity for entity in nearby
        if "CHOP_workable" in entity.get("tags", []) and float(entity.get("distance", 999)) <= 6
    ]
    mine_targets = [
        entity for entity in nearby
        if "MINE_workable" in entity.get("tags", []) and float(entity.get("distance", 999)) <= 6
    ]
    attackable_targets = [
        entity for entity in nearby
        if entity.get("attackable") is True and float(entity.get("distance", 999)) <= 8
    ]
    pursuing_targets = [
        entity for entity in nearby
        if entity.get("activeThreat") is True and float(entity.get("distance", 999)) <= 12
    ]
    weapon_equipped, weapon_carried, best_weapon = weapon_status(state)
    axe_uses = conservative_tool_uses(state, "axe")
    pickaxe_uses = conservative_tool_uses(state, "pickaxe")
    eligible_chop_targets = [
        entity for entity in chop_targets
        if entity.get("work_required") is not None
        and axe_uses >= int(entity["work_required"])
    ]
    eligible_mine_targets = [
        entity for entity in mine_targets
        if entity.get("work_required") is not None
        and pickaxe_uses >= int(entity["work_required"])
    ]
    if  eligible_chop_targets:
        target = min(eligible_chop_targets, key=lambda entity: float(entity.get("distance", 999)))
        required = int(target["work_required"])
        criteria["chop_nearest_tree"] = (
            f"Complete the nearest tree-like target ({target.get('prefab')}) and then pick up "
            "nearby drops such as logs and pinecones within 4 units of that tree (up to 12 stacks). "
            f"It is {float(target.get('distance', 0)):.2f} units away and is budgeted for {required} chops; "
            f"the selected axe has at least {axe_uses} uses left. This is one longer action, "
            "not a single chop."
        )
        dispatch["chop_nearest_tree"] = {"kind": "chop_nearest_tree"}
    if  eligible_mine_targets:
        target = min(eligible_mine_targets, key=lambda entity: float(entity.get("distance", 999)))
        required = int(target["work_required"])
        criteria["mine_nearest_rock"] = (
            f"Complete the nearest rock target ({target.get('prefab')}) and then pick up "
            "nearby drops such as rocks, flint, nitre, and gold within 4 units of that rock (up to 12 stacks). "
            f"It is {float(target.get('distance', 0)):.2f} units away and requires at most {required} strikes; "
            f"the selected pickaxe has at least {pickaxe_uses} uses left. This is one longer action, "
            "not a single strike."
        )
        dispatch["mine_nearest_rock"] = {"kind": "mine_nearest_rock"}
    if pursuing_targets:
        target = min(pursuing_targets, key=lambda entity: float(entity.get("distance", 999)))
        threat_distance = float(target.get("distance", 999))
        criteria["flee_from_nearest_hostile"] = (
            f"Move directly away from the nearest pursuing hostile ({target.get('prefab')}) at {threat_distance:.2f} units "
            "in repeated escape legs until no active pursuer remains nearby or the bounded safety limit is reached. "
            "Strongly prefer when unarmed, badly hurt, or fighting is unnecessary."
        )
        dispatch["flee_from_nearest_hostile"] = {
            "kind": "flee_from_nearest_hostile",
            "guid": target.get("guid"),
        }
    if attackable_targets:
        target = min(attackable_targets, key=lambda entity: float(entity.get("distance", 999)))
        threat_distance = float(target.get("distance", 999))
        if weapon_carried and not weapon_equipped:
            criteria["equip_weapon"] = (
                f"Equip the best carried weapon ({best_weapon}) before fighting. The hostile is "
                f"{threat_distance:.2f} units away. Prefer fleeing instead if there is no safe time to equip."
            )
            dispatch["equip_weapon"] = {"kind": "equip_weapon"}
        if weapon_equipped and threat_distance <= 6.0:
            vitals_for_combat = state.get("player", {}).get("vitals", {})
            health = float(vitals_for_combat.get("health") or 0)
            health_max = max(float(vitals_for_combat.get("health_max") or 1), 1)
            criteria["attack_nearest_hostile"] = (
                f"Attack the nearest hostile ({target.get('prefab')}) once with the equipped weapon at "
                f"{threat_distance:.2f} units. Health is {health:.1f}/{health_max:.1f}; prefer fleeing at low health "
                "or when combat is not necessary. Attackable alone does not prove a creature is hostile; "
                "do not choose this for a neutral target."
            )
            dispatch["attack_nearest_hostile"] = {"kind": "attack_nearest_hostile"}

    vitals = state.get("player", {}).get("vitals", {})
    hunger = float(vitals.get("hunger") or 0)
    hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
    fire = nearest_cookable_fire(state)
    raw_food = [prefab for prefab in COOKABLE_FOOD_PRODUCTS if counts.get(prefab, 0) > 0]
    cook_then_eat = fire is not None and bool(raw_food) and not any(
        counts.get(prefab, 0) > 0 for prefab in COOKABLE_FOOD_PRODUCTS.values()
    )
    if can_eat_safe_food(state) and hunger / hunger_max < 0.80:
        urgency = (
            "Hunger is critical: eat immediately; waiting or optional collection risks starvation. "
            if hunger / hunger_max < CRITICAL_HUNGER_RATIO else ""
        )
        if cook_then_eat:
            eating_plan = (
                "With no preferred cooked food carried and a nearby lit cooker, cook one item first "
                "and then eat the cooked food as one action. If cooking fails, safely edible "
                "raw berries or carrots are a fallback; raw meat is not."
            )
        else:
            eating_plan = "Eat one available safe food item directly; no separate cooking step is needed."
        criteria["eat_safe_food"] = (
            f"{urgency}Hunger is {hunger:.1f}/{hunger_max:.1f}. {eating_plan}"
        )
        dispatch["eat_safe_food"] = {"kind": "eat_safe_food"}

    if fire is not None and raw_food:
        criteria["cook_food"] = (
            f"Cook one carried raw food item on the nearby lit {fire['prefab']} "
            f"{float(fire.get('distance', 0)):.2f} units away. Available raw foods: "
            f"{', '.join(raw_food)}. The action finishes only after a cooked item appears in inventory. "
            "Choose this to prepare food for later; when the immediate goal is to eat, "
            "choose eat_safe_food because it handles cooking if needed."
        )
        dispatch["cook_food"] = {"kind": "cook_food"}

    if crafting.get("campfire", False):
        criteria["build_campfire"] = (
            "Build a campfire at the nearest valid open position. Exposed only because the recipe is craftable, "
            "it is night, and no lit campfire or firepit is nearby."
        )
        dispatch["build_campfire"] = {"kind": "build_campfire"}

    if crafting.get("sciencemachine", False):
        criteria["build_sciencemachine"] = (
            "Build a science machine at the nearest valid open position. Exposed only because the recipe is craftable."
        )
        dispatch["build_sciencemachine"] = {"kind": "build_sciencemachine"}
    
    criteria["wait"] = (
        "Take no action for one telemetry interval. Choose only when no currently useful "
        "safe action is available or waiting is safer than acting."
    )
    dispatch["wait"] = {"kind": "wait"}

    # filter out any actions that are blocked by hard safety rules
    criteria = {
        k: v for k, v in criteria.items()
        if action_allowed(state, dispatch[k]["kind"])[0]
    }
    dispatch = {k: v for k, v in dispatch.items() if k in criteria}
    return criteria, dispatch


def compact_game_state(state: dict, daily_plan: Optional[dict] = None) -> dict:
    world = state.get("world", {})
    player = state.get("player", {})
    vitals = player.get("vitals", {})
    counts = inventory_counts(state)
    hunger = float(vitals.get("hunger") or 0)
    hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
    weapon_equipped, weapon_carried, best_weapon = weapon_status(state)
    work = state.get("work") or {}
    compact = {
        "goal": "Survive indefinitely and steadily improve food, light, tools, resources, and safety.",
        "hard_rules": [
            "Choose only an action listed in the available criteria; unavailable actions cannot be executed.",
            "At night without an equipped torch, exploration and collection are blocked even beside a stationary fire; fleeing an immediate threat is an exception.",
            "A torch can be equipped only at night and only when no lit campfire or firepit is nearby. A campfire can be built only at night when no lit fire is nearby.",
        ],
        "decision_guidance": [
            "Assess immediate threats, darkness, and starvation before optional progress; compare the remaining safe actions rather than following a fixed script.",
            "eat_safe_food is a complete eating action: if no preferred cooked food is carried and a lit cooker is nearby, it cooks one item and then eats; cook_food alone only prepares food for later.",
            "craft_torch is a complete crafting action: if DST auto-equips the new torch before night, the controller unequips it to preserve fuel.",
            "chop_nearest_tree and mine_nearest_rock include bounded pickup of nearby work drops, including secondary materials. Collect leftovers separately only when useful items remain visible.",
            "At night near a lit fire, unequip a torch to save fuel only if staying near that fire; keep it equipped when moving away.",
            "Prefer a useful safe action over repeated waiting, but do not take unnecessary risks for optional resources.",
            "A daily_plan is strategic advice from a separate planner, not a command. Make the fastest safe next-action choice from current facts; do not generate or revise a plan.",
        ],
        "day": world.get("day"),
        "phase": world.get("phase"),
        "world_time": world.get("time"),
        "health": vitals.get("health"),
        "hunger": vitals.get("hunger"),
        "hunger_max": vitals.get("hunger_max"),
        "hunger_fraction": round(hunger / hunger_max, 3),
        "sanity": vitals.get("sanity"),
        "dead": vitals.get("dead"),
        "position": player.get("position"),
        "navigation": state.get("navigation", {}),
        "inventory": counts,
        "safe_food_count": sum(counts.get(prefab, 0) for prefab in SAFE_FOOD_PREFABS),
        "cookable_raw_count": sum(counts.get(prefab, 0) for prefab in COOKABLE_FOOD_PRODUCTS),
        "nearby_cooker": nearest_cookable_fire(state) is not None,
        "last_work": {
            "action": work.get("action"),
            "status": work.get("status"),
        } if work else None,
        "torch_equipped": has_equipped_torch(state),
        "nearby_lit_fire": has_nearby_lit_fire(state),
        "weapon_equipped": weapon_equipped,
        "weapon_carried": weapon_carried,
        "best_weapon": best_weapon,
        "can_craft_torch": state.get("crafting", {}).get("torch", False),
        "can_craft_axe": state.get("crafting", {}).get("axe", False),
        "can_craft_pickaxe": state.get("crafting", {}).get("pickaxe", False),
        "can_craft_campfire": state.get("crafting", {}).get("campfire", False),
        "nearby": [
            {
                "prefab": entity.get("prefab"),
                "distance": entity.get("distance"),
                "tags": entity.get("tags", []),
                "pickable": entity.get("pickable"),
                "work_required": entity.get("work_required"),
                "attackable": entity.get("attackable", False),
                "activeThreat": entity.get("activeThreat", False),
            }
            for entity in state.get("nearby", [])
        ],
    }
    if daily_plan is not None:
        compact["daily_plan"] = daily_plan
    return compact


def call_jev(
    api_url: str, api_key: str, model: str, state: dict,
    criteria: Dict[str, str], daily_plan: Optional[dict] = None,
) -> dict:
    body = {
        "state": json.dumps(compact_game_state(state, daily_plan), ensure_ascii=False, separators=(",", ":")),
        "model": model,
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one action key from the supplied criteria. Treat each key as "
                    "one bounded action, even when the controller performs several verified substeps. "
                    "Decide the next action quickly from current state and criteria; do not plan ahead. "
                    "Obey hard rules and immediate survival needs. Treat daily_plan, when present, "
                    "as optional strategic guidance, never as permission to ignore current facts. "
                    "Do not request unavailable or redundant substeps."
                ),
                "criteria": criteria,
            }
        },
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "jev-dst-agent/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"JEV API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"JEV API connection failed: {exc.reason}") from exc

    answer = payload.get("answers", {}).get("next_action")
    if not isinstance(answer, dict) or not answer.get("choice"):
        raise RuntimeError("JEV response did not contain answers.next_action.choice")
    return {"response": payload, "answer": answer}


def execute_action(action: dict, log_path: Path, move_seconds: float = 1.0) -> bool:
    kind = action["kind"]
    if kind == "collect":
        return controller.collect(log_path, action["prefab"], preferred_guid=action.get("guid"))
    elif kind == "explore":
        if action.get("navigation_mode") == "road":
            return controller.follow_road(
                log_path,
                float(action["frontier_x"]),
                float(action["frontier_z"]),
                float(action["leg_distance"]),
                move_seconds,
            )
        return controller.explore_leg(
            log_path,
            float(action["target_x"]),
            float(action["target_z"]),
            float(action["leg_distance"]),
            move_seconds,
            frontier_x=float(action["frontier_x"]),
            frontier_z=float(action["frontier_z"]),
            navigation_mode=action.get("navigation_mode", "region"),
        )
    elif kind == "wait":
        time.sleep(1.0)
    elif kind == "craft_torch":
        controller.craft_torch(log_path)
    elif kind == "equip_torch":
        controller.equip_torch(log_path)
    elif kind == "unequip_torch":
        controller.unequip_torch(log_path)
    elif kind == "craft_axe":
        controller.craft_inventory_item(log_path, "axe", "craft_axe")
    elif kind == "craft_pickaxe":
        controller.craft_inventory_item(log_path, "pickaxe", "craft_pickaxe")
    elif kind == "build_campfire":
        controller.build_campfire(log_path)
    elif kind == "build_sciencemachine":
        controller.build_sciencemachine(log_path)
    elif kind == "eat_safe_food":
        controller.eat_safe_food(log_path)
    elif kind == "cook_food":
        controller.cook_food(log_path)
    elif kind == "chop_nearest_tree":
        controller.complete_work_action(log_path, kind, "CHOP_workable")
    elif kind == "mine_nearest_rock":
        controller.complete_work_action(log_path, kind, "MINE_workable")
    elif kind == "attack_nearest_hostile":
        controller.force_attack(log_path)
    elif kind == "equip_weapon":
        controller.equip_weapon(log_path)
    elif kind == "flee_from_nearest_hostile":
        controller.flee_from_hostile(log_path, preferred_guid=action.get("guid"))
    else:
        raise RuntimeError(f"Unsupported bounded action kind: {kind}")
    return True


def default_execution_threshold(choice: str, state: Optional[dict] = None) -> float:
    """Apply the shared gate, with only urgent and riskier actions excepted."""
    if choice in {"flee_from_nearest_hostile", "wait"}:
        return 0.0
    if choice in {"explore", "explore_current_region", "explore_other_region"}:
        return 0.15
    if choice == "attack_nearest_hostile":
        return 0.45
    if choice == "eat_safe_food" and state is not None:
        vitals = state.get("player", {}).get("vitals", {})
        hunger = float(vitals.get("hunger") or 0)
        hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
        if hunger / hunger_max < CRITICAL_HUNGER_RATIO:
            return 0.0
    return 0.25


def run_decision_cycle(
    args, api_url: str, api_key: str, model: str,
    planner: Optional[DailyPlanner] = None,
) -> bool:
    """Run one sense-decide-act cycle. Return True at a terminal game state."""
    state, _ = controller.latest_state(args.log)
    world = state.get("world", {})
    vitals = state.get("player", {}).get("vitals", {})
    if vitals.get("dead"):
        print("goal_failed: player is dead; control loop stopped")
        return True
    daily_plan = planner.update(state) if planner is not None else None
    criteria, dispatch = build_candidates(state)
    result = call_jev(api_url, api_key, model, state, criteria, daily_plan)
    answer = result["answer"]
    choice = answer["choice"]
    confidence = float(answer.get("confidence", 0))
    print(
        f"day={world.get('day')} phase={world.get('phase')} "
        f"model={result['response'].get('model', model)} "
        f"choice={choice} confidence={confidence:.3f}"
    )
    if args.show_probabilities:
        print(json.dumps(answer.get("probabilities", {}), indent=2, sort_keys=True))

    if choice not in dispatch:
        raise RuntimeError(f"JEV selected unknown action: {choice}")
    if not args.execute:
        print("dry_run: no game input sent")
        return False

    threshold = (
        args.min_confidence
        if args.min_confidence is not None
        else default_execution_threshold(choice, state)
    )
    print(f"execution_threshold={threshold:.2f}")
    if confidence < threshold:
        print(f"abstained: confidence below {threshold:.2f}")
        return False

    try:
        executed = execute_action(dispatch[choice], args.log, args.move_seconds)
    except (RuntimeError, OSError, ValueError):
        if planner is not None:
            planner.record_action(state, choice, "failed")
        raise
    if planner is not None:
        planner.record_action(state, choice, "executed" if executed else "skipped")
    print("action_executed" if executed else "action_skipped: state changed before execution")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, help="Path to the .env file")
    parser.add_argument("--log", type=Path, default=controller.DEFAULT_LOG)
    parser.add_argument("--execute", action="store_true", help="Execute the selected action")
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="Override the action-specific execution threshold",
    )
    parser.add_argument("--show-probabilities", action="store_true")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously sense, ask JEV, and optionally execute until death or interruption",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Seconds to wait after each completed cycle (default: 0, no cooldown)",
    )
    parser.add_argument(
        "--move-seconds",
        type=float,
        default=1.0,
        help="Duration of each exploratory movement (default: 1.0, range: 0.2-2.0)",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sample = {
            "world": {"day": 1, "phase": "day", "time": 0.2},
            "player": {"vitals": {"health": 150, "hunger": 140, "sanity": 200}, "inventory": {"items": []}},
            "crafting": {"torch": False},
            "nearby": [
                {
                    "guid": 101,
                    "prefab": "grass",
                    "distance": 3.0,
                    "tags": ["pickable"],
                    "pickable": True,
                }
            ],
        }
        criteria, dispatch = build_candidates(sample)
        assert "collect_grass" in criteria
        assert dispatch["collect_grass"] == {
            "kind": "collect",
            "prefab": "grass",
            "guid": 101,
        }
        depleted = dict(sample)
        depleted["nearby"] = [
            {"prefab": "grass", "distance": 1.0, "tags": [], "pickable": False},
            {"prefab": "sapling", "distance": 1.0, "tags": [], "pickable": False},
            {"prefab": "berrybush", "distance": 1.0, "tags": [], "pickable": False},
        ]
        depleted_criteria, _ = build_candidates(depleted)
        assert "collect_grass" not in depleted_criteria
        assert "collect_sapling" not in depleted_criteria
        assert "collect_berrybush" not in depleted_criteria
        loose_resources = dict(sample)
        loose_resources["nearby"] = [
            {"guid": 102, "prefab": "log", "distance": 2.0, "tags": ["_inventoryitem"]},
            {"guid": 103, "prefab": "rocks", "distance": 2.5, "tags": ["_inventoryitem"]},
        ]
        loose_criteria, loose_dispatch = build_candidates(loose_resources)
        assert loose_dispatch["collect_log"] == {"kind": "collect", "prefab": "log", "guid": 102}
        assert loose_dispatch["collect_rocks"] == {"kind": "collect", "prefab": "rocks", "guid": 103}
        assert compact_game_state(sample)["goal"].startswith("Survive")
        assert default_execution_threshold("explore_current_region") == 0.15
        assert default_execution_threshold("explore_other_region") == 0.15
        assert default_execution_threshold("collect_grass") == 0.25
        assert default_execution_threshold("craft_torch") == 0.25
        assert default_execution_threshold("build_campfire") == 0.25
        assert default_execution_threshold("wait") == 0.0
        assert "craft_torch" not in criteria
        craftable = dict(sample)
        craftable["crafting"] = {"torch": True}
        craft_criteria, craft_dispatch = build_candidates(craftable)
        assert "craft_torch" in craft_criteria
        assert craft_dispatch["craft_torch"] == {"kind": "craft_torch"}
        equipped = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {"torch": False},
            "player": {"inventory": {"items": [], "equipped": [{"prefab": "torch", "count": 1}]}},
            "nearby": [],
        }
        equipped_criteria, equipped_dispatch = build_candidates(equipped)
        assert "unequip_torch" in equipped_criteria
        assert equipped_dispatch["unequip_torch"] == {"kind": "unequip_torch"}
        equipped["world"]["phase"] = "night"
        night_criteria, _ = build_candidates(equipped)
        assert "unequip_torch" not in night_criteria
        survival = {
            "world": {"day": 1, "phase": "night"},
            "crafting": {"torch": False, "axe": True, "pickaxe": True, "campfire": True, "sciencemachine": False},
            "player": {
                "vitals": {"health": 150, "hunger": 50, "hunger_max": 150, "sanity": 200},
                "inventory": {"items": [{"prefab": "berries", "count": 2}], "equipped": []},
            },
            "nearby": [],
        }
        survival_criteria, _ = build_candidates(survival)
        assert "build_campfire" in survival_criteria
        assert "craft_torch" not in survival_criteria
        assert "build_sciencemachine" not in survival_criteria
        durable = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {},
            "player": {
                "vitals": {"health": 150, "hunger": 100, "hunger_max": 150, "sanity": 200},
                "inventory": {
                    "items": [{"prefab": "axe", "count": 1, "durability_percent": 0.15}],
                    "equipped": [],
                },
            },
            "nearby": [
                {"prefab": "evergreen", "distance": 3, "tags": ["CHOP_workable"], "work_required": 15}
            ],
        }
        durable_criteria, _ = build_candidates(durable)
        assert "chop_nearest_tree" in durable_criteria
        durable["player"]["inventory"]["items"][0]["durability_percent"] = 0.14
        worn_criteria, _ = build_candidates(durable)
        assert "chop_nearest_tree" not in worn_criteria
        threat = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {},
            "player": {
                "vitals": {"health": 150, "hunger": 100, "hunger_max": 150, "sanity": 200},
                "inventory": {"items": [], "equipped": []},
            },
            "nearby": [
                {"guid": 201, "prefab": "tallbird", "distance": 4.0, "tags": [], "activeThreat": True, "attackable": True},
            ],
        }
        threat_criteria, threat_dispatch = build_candidates(threat)
        assert "flee_from_nearest_hostile" in threat_criteria
        assert threat_dispatch["flee_from_nearest_hostile"]["guid"] == 201
        assert "equip_weapon" not in threat_criteria
        assert "attack_nearest_hostile" not in threat_criteria
        print("self-test passed")
        return 0

    env_path = resolve_env_path(args.env)
    load_dotenv(env_path)
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError(f"JEV_API_KEY is missing or empty in {env_path}")
    api_url = os.environ.get("JEV_API_URL", DEFAULT_API_URL)
    model = os.environ.get("JEV_MODEL", DEFAULT_MODEL)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    planner = DailyPlanner(
        deepseek_key,
        os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
        os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
    ) if deepseek_key and args.loop and args.execute else None
    if not 0.0 <= args.interval <= 60.0:
        raise ValueError("--interval must be between 0 and 60 seconds")
    if not 0.2 <= args.move_seconds <= 2.0:
        raise ValueError("--move-seconds must be between 0.2 and 2.0 seconds")

    if args.loop:
        mode = "execute" if args.execute else "dry-run"
        print(f"control_loop_started mode={mode} interval={args.interval:.1f}s")
        if planner is None and args.execute:
            print("daily_planner_disabled: DEEPSEEK_API_KEY not set")

    cycle = 0
    consecutive_errors = 0
    while True:
        cycle += 1
        if args.loop:
            print(f"cycle={cycle}")
        try:
            terminal = run_decision_cycle(args, api_url, api_key, model, planner)
            consecutive_errors = 0
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            if not args.loop:
                raise
            consecutive_errors += 1
            retry_base = max(args.interval, 1.0)
            retry_delay = min(retry_base * (2 ** min(consecutive_errors - 1, 3)), 30.0)
            print(
                f"cycle_error={exc}; retrying_in={retry_delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(retry_delay)
            continue

        if terminal or not args.loop:
            if planner is not None:
                planner.close()
            return 0
        if args.interval > 0:
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        controller.release_movement_keys()
        print("control_loop_stopped: keyboard interrupt")
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
