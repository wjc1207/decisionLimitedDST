"""Bounded, telemetry-verified controls for the JEV DST Agent client mod."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import telemetry


PREFIX = "[JEV_DST_STATE]"
DEFAULT_LOG = Path.home() / "Documents" / "Klei" / "DoNotStarveTogether" / "client_log.txt"
GAME_EXE_FRAGMENT = "dontstarve_steam"

VK = {
    "ctrl": 0x11,     # Control
    "attack": 0x46,   # F
    "up": 0x57,       # W
    "left": 0x41,     # A
    "down": 0x53,     # S
    "right": 0x44,    # D
    "interact": 0x20, # Space
    "craft_torch": 0x6A, # Numpad multiply
    "equip_torch": 0x6F, # Numpad divide
    "unequip_torch": 0x6D, # Numpad minus
    "craft_axe": 0x6B, # Numpad plus
    "craft_pickaxe": 0x6E, # Numpad period
    "build_campfire": 0x75, # F6
    "eat_safe_food": 0x76, # F7
    "chop_nearest_tree": 0x77, # F8
    "mine_nearest_rock": 0x78, # F9
    "equip_weapon": 0x7A, # F11
    "build_sciencemachine": 0x7B, # F12
    "reject_frontier": 0x74, # F5
    "reject_road": 0x73, # F4
    "cook_food": 0x72, # F3
}
MOVEMENT_KEYS = (VK["up"], VK["left"], VK["down"], VK["right"])
EXPLORATION_STALL = {"target": None, "count": 0, "last_position": None}
GRAPH_ROUTE_BLACKLIST: Dict[str, float] = {}
REGION_ARRIVAL_DISTANCE = 1.5
ROAD_ARRIVAL_DISTANCE = 1.25
EXPLORATION_HAZARD_CLEARANCE = 14.0
EXPLORATION_HAZARD_CAUTION = 20.0


def graph_route_available(node_id: str) -> bool:
    return time.monotonic() >= GRAPH_ROUTE_BLACKLIST.get(node_id, 0.0)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    # INPUT's union must include its largest member (MOUSEINPUT). Omitting it
    # makes sizeof(INPUT) 32 instead of the required 40 bytes on 64-bit Windows,
    # which causes SendInput to fail with ERROR_INVALID_PARAMETER (87).
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_game_window() -> int:
    matches: List[int] = []

    @WNDENUMPROC
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if GAME_EXE_FRAGMENT in Path(process_path(pid.value)).name.lower():
            matches.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        raise RuntimeError("DST game window was not found")
    return matches[0]


def focus_game(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.12)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("DST window could not be focused; click the game and retry")


def send_key(vk: int, down: bool) -> None:
    event = INPUT(
        type=INPUT_KEYBOARD,
        u=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, 0)),
    )
    if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def release_movement_keys() -> None:
    for key in MOVEMENT_KEYS:
        send_key(key, False)


def tap(keys: Sequence[int], seconds: float) -> None:
    seconds = min(max(seconds, 0.04), 2.0)
    try:
        for key in keys:
            send_key(key, True)
        time.sleep(seconds)
    finally:
        for key in reversed(keys):
            send_key(key, False)


def parse_state_line(line: str) -> Optional[dict]:
    return telemetry.StateAssembler().feed(line)


def latest_state(log_path: Path) -> Tuple[dict, int]:
    return telemetry.latest_state(log_path)


def wait_for_fresh_state(log_path: Path, old_position: int, timeout: float = 3.0) -> Tuple[dict, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, position = latest_state(log_path)
        if position > old_position:
            return state, position
        time.sleep(0.08)
    raise RuntimeError("Timed out waiting for fresh telemetry")


def wait_for_state_condition(log_path: Path, old_position: int, predicate, timeout: float = 6.0) -> Tuple[dict, int]:
    deadline = time.monotonic() + timeout
    position = old_position
    last_state: Optional[dict] = None
    while time.monotonic() < deadline:
        state, new_position = latest_state(log_path)
        if new_position > position:
            position = new_position
            last_state = state
            if predicate(state):
                return state, position
        time.sleep(0.08)
    detail = "" if last_state is None else f"; last state: {status_text(last_state)}"
    raise RuntimeError(f"Timed out verifying semantic action{detail}")


def find_target(state: dict, prefab: str, guid: Optional[int] = None) -> Optional[dict]:
    candidates = []
    for entity in state.get("nearby", []):
        if guid is not None and entity.get("guid") == guid:
            return entity
        if guid is None and entity.get("prefab", "").lower() == prefab.lower():
            candidates.append(entity)
    return min(candidates, key=lambda entity: entity.get("distance", math.inf), default=None)


PICKABLE_SOURCE_PREFABS = {
    "grass",
    "sapling",
    "sapling_moon",
    "berrybush",
    "berrybush2",
    "juicyberrybush",
}

COLLECT_PRODUCTS = {
    "grass": "cutgrass",
    "sapling": "twigs",
    "sapling_moon": "twigs",
    "berrybush": "berries",
    "berrybush2": "berries",
    "juicyberrybush": "berries_juicy",
}
ROAD_INTEREST_PREFABS = set(COLLECT_PRODUCTS) | {
    "twigs", "cutgrass", "flint", "log", "rocks", "goldnugget",
    "nitre", "pinecone", "acorn", "charcoal", "marble", "moonrocknugget",
    "berries", "carrot", "carrot_planted", "seeds",
}
WORK_DROP_PREFABS = {
    "CHOP_workable": frozenset({
        "log", "pinecone", "acorn", "charcoal", "livinglog", "twigs",
    }),
    "MINE_workable": frozenset({
        "rocks", "flint", "nitre", "goldnugget", "marble", "moonrocknugget",
        "ice", "redgem", "bluegem", "purplegem", "thulecite", "thulecite_pieces",
    }),
}


def collect_target_available(target: Optional[dict]) -> bool:
    if target is None:
        return False
    if target.get("prefab") in PICKABLE_SOURCE_PREFABS:
        return target.get("pickable") is True
    tags = set(target.get("tags", []))
    return "_inventoryitem" in tags or "pickable" in tags


def find_collectible_target(
    state: dict,
    prefab: str,
    preferred_guid: Optional[int] = None,
) -> Optional[dict]:
    candidates = [
        entity
        for entity in state.get("nearby", [])
        if entity.get("prefab", "").lower() == prefab.lower()
        and collect_target_available(entity)
    ]
    if preferred_guid is not None:
        preferred = next(
            (entity for entity in candidates if entity.get("guid") == preferred_guid),
            None,
        )
        if preferred is not None:
            return preferred
    return min(candidates, key=lambda entity: entity.get("distance", math.inf), default=None)


def collect_target_completed(original: dict, current: Optional[dict]) -> bool:
    if current is None:
        return True
    if original.get("prefab") in PICKABLE_SOURCE_PREFABS:
        return current.get("pickable") is not True
    original_tags = set(original.get("tags", []))
    current_tags = set(current.get("tags", []))
    completion_tags = original_tags.intersection({"pickable", "_inventoryitem"})
    return bool(completion_tags) and not completion_tags.issubset(current_tags)


def movement_keys_for_delta(state: dict, dx: float, dz: float) -> List[int]:
    camera = state.get("camera", {})
    right = camera.get("right")
    forward = camera.get("forward")
    if not right or not forward:
        raise RuntimeError("Telemetry has no camera vectors; install mod version 0.2.0")

    horizontal = dx * float(right["x"]) + dz * float(right["z"])
    vertical = dx * float(forward["x"]) + dz * float(forward["z"])
    largest = max(abs(horizontal), abs(vertical), 1e-9)

    keys: List[int] = []
    if vertical > 0.30 * largest:
        keys.append(VK["up"])
    elif vertical < -0.30 * largest:
        keys.append(VK["down"])
    if horizontal > 0.30 * largest:
        keys.append(VK["right"])
    elif horizontal < -0.30 * largest:
        keys.append(VK["left"])
    return keys


def movement_keys_for_target(state: dict, target: dict) -> List[int]:
    return movement_keys_for_delta(state, float(target["dx"]), float(target["dz"]))


def approach_step_duration(distance: float) -> float:
    """Use longer bounded pulses for distant targets and brake near interaction range."""
    return min(1.0, max(0.18, (distance - 0.75) / 4.5))


def nearest_hostile(state: dict, maximum_distance: float = 8.0) -> Optional[dict]:
    return min(
        (
            entity
            for entity in state.get("nearby", [])
            if entity.get("activeThreat") is True
            and float(entity.get("distance", math.inf)) <= maximum_distance
        ),
        key=lambda entity: float(entity.get("distance", math.inf)),
        default=None,
    )


def exploration_hazard_on_leg(
    state: dict, target_x: float, target_z: float,
) -> Optional[dict]:
    """Reject an exploration leg that crosses or approaches a known hostile."""
    position = state.get("player", {}).get("position", {})
    if position.get("x") is None or position.get("z") is None:
        return None
    px, pz = float(position["x"]), float(position["z"])
    dx, dz = target_x - px, target_z - pz
    length_sq = dx * dx + dz * dz
    hazards: dict[object, dict] = {}
    for hazard in state.get("navigation", {}).get("hazards", []):
        if hazard.get("x") is not None and hazard.get("z") is not None:
            hazards[hazard.get("guid") or (hazard["x"], hazard["z"])] = hazard
    for entity in state.get("nearby", []):
        if entity.get("potentialThreat") is True or entity.get("activeThreat") is True:
            if entity.get("dx") is not None and entity.get("dz") is not None:
                hazards[entity.get("guid") or (entity["dx"], entity["dz"])] = {
                    "guid": entity.get("guid"), "prefab": entity.get("prefab"),
                    "x": px + float(entity["dx"]), "z": pz + float(entity["dz"]),
                }
    for hazard in hazards.values():
        hx, hz = float(hazard["x"]), float(hazard["z"])
        start_distance = math.hypot(px - hx, pz - hz)
        end_distance = math.hypot(target_x - hx, target_z - hz)
        projection = 0.0 if length_sq == 0 else max(0.0, min(
            1.0, ((hx - px) * dx + (hz - pz) * dz) / length_sq,
        ))
        path_distance = math.hypot(px + projection * dx - hx, pz + projection * dz - hz)
        moving_away = (
            start_distance < EXPLORATION_HAZARD_CLEARANCE
            and end_distance > start_distance + 0.5
            and path_distance >= start_distance - 0.5
        )
        if not moving_away and (
            path_distance < EXPLORATION_HAZARD_CLEARANCE
            or (end_distance < EXPLORATION_HAZARD_CAUTION
                and end_distance < start_distance - 0.5)
        ):
            return hazard
    return None


def choose_escape_route(
    state: dict, blocked_keys: set[tuple[int, ...]] | None = None,
) -> tuple[dict, List[int], float] | None:
    """Choose a passable escape ray that maximizes separation from pursuers."""
    blocked_keys = blocked_keys or set()
    threats = [
        entity for entity in state.get("nearby", [])
        if entity.get("activeThreat") is True
        and float(entity.get("distance", math.inf)) <= 12.0
    ]
    if not threats:
        return None
    routes = state.get("navigation", {}).get("escape_routes")
    if routes is None:
        raise RuntimeError("Escape routes missing from telemetry; install and enable Mod v1.4.1 or newer")
    best = None
    best_score = -math.inf
    current_separation = min(float(threat.get("distance", math.inf)) for threat in threats)
    for route in routes:
        distance = float(route.get("distance") or 0)
        dx, dz = float(route.get("dx") or 0), float(route.get("dz") or 0)
        norm = math.hypot(dx, dz)
        if distance < 1.5 or norm < 0.5:
            continue
        dx, dz = dx / norm, dz / norm
        keys = movement_keys_for_delta(state, dx, dz)
        if not keys or tuple(keys) in blocked_keys:
            continue
        travel = min(distance, 5.0)
        separation = min(
            math.hypot(float(threat["dx"]) - dx * travel,
                       float(threat["dz"]) - dz * travel)
            for threat in threats
        )
        gain = separation - current_separation
        score = separation + 0.25 * gain + 0.04 * distance
        if score > best_score:
            best = (route, keys, gain)
            best_score = score
    return best


def has_nearby_lit_fire(state: dict, maximum_distance: float = 8.0) -> bool:
    return any(
        entity.get("prefab") in {"campfire", "firepit"}
        and "fire" in entity.get("tags", [])
        and float(entity.get("distance", math.inf)) <= maximum_distance
        for entity in state.get("nearby", [])
    )


def night_travel_without_torch(state: dict) -> bool:
    return (
        state.get("world", {}).get("phase") == "night"
        and item_count(state, "torch", equipped_only=True) == 0
    )


def status_text(state: dict) -> str:
    player = state.get("player", {})
    world = state.get("world", {})
    vitals = player.get("vitals", {})
    return (
        f"character={player.get('prefab')} day={world.get('day')} phase={world.get('phase')} "
        f"health={vitals.get('health')} hunger={vitals.get('hunger')} sanity={vitals.get('sanity')} "
        f"nearby={len(state.get('nearby', []))}"
    )


def explore_leg(
    log_path: Path,
    target_x: float,
    target_z: float,
    leg_distance: float,
    full_leg_seconds: float = 1.0,
    frontier_x: Optional[float] = None,
    frontier_z: Optional[float] = None,
    navigation_mode: str = "region",
    target_node_id: Optional[str] = None,
) -> bool:
    def section(observed: dict) -> dict:
        navigation = observed.get("navigation", {})
        if navigation_mode == "region":
            return navigation
        if navigation_mode == "road":
            return navigation.get("road") or {}
        graph = navigation.get("graph") or {}
        routes = [*(graph.get("adjacent") or []), graph.get("return_route") or {}]
        for route in routes:
            if route.get("node_id") == target_node_id:
                return route
        return {}

    state, log_position = latest_state(log_path)
    if night_travel_without_torch(state):
        print("explore leg skipped: night requires an equipped torch, even beside a lit fire")
        return False
    if navigation_mode not in {"region", "road", "graph"}:
        raise ValueError(f"Unknown navigation mode: {navigation_mode}")
    navigation = section(state)
    frontier = navigation.get("target") or {}
    if navigation.get("status") != "ready" or not frontier:
        print("explore leg skipped: frontier is no longer available")
        return False
    frontier_key = (round(float(frontier["x"]), 2), round(float(frontier["z"]), 2))
    if frontier_x is not None and frontier_z is not None:
        expected_key = (round(frontier_x, 2), round(frontier_z, 2))
        if frontier_key != expected_key:
            print("explore leg skipped: frontier changed since JEV decision")
            return False
    current_leg = navigation.get("leg") or {}
    if current_leg.get("x") is None or current_leg.get("z") is None:
        print("explore leg skipped: no current path to the frontier")
        return False
    current_x = float(current_leg["x"])
    current_z = float(current_leg["z"])
    if math.hypot(current_x - target_x, current_z - target_z) > 0.2:
        print("explore leg updated to the current passable path")
    target_x = current_x
    target_z = current_z
    leg_distance = float(current_leg.get("distance") or leg_distance)
    position = state.get("player", {}).get("position", {})
    if position.get("x") is None or position.get("z") is None:
        raise RuntimeError("Telemetry has no player position for frontier exploration")
    start_x = float(position["x"])
    start_z = float(position["z"])
    stall_key = (navigation_mode, target_node_id, *frontier_key)
    if EXPLORATION_STALL["target"] != stall_key:
        EXPLORATION_STALL.update(target=stall_key, count=0, last_position=None)
    previous_position = EXPLORATION_STALL["last_position"]
    if previous_position is not None and math.hypot(
        start_x - previous_position[0], start_z - previous_position[1]
    ) >= 0.2:
        EXPLORATION_STALL["count"] = 0
    dx = target_x - float(position["x"])
    dz = target_z - float(position["z"])
    remaining = math.hypot(dx, dz)
    arrival_distance = ROAD_ARRIVAL_DISTANCE if navigation_mode == "road" else REGION_ARRIVAL_DISTANCE
    if remaining <= arrival_distance:
        print("explore leg skipped: leg target is within arrival tolerance")
        return False
    hazard = exploration_hazard_on_leg(state, target_x, target_z)
    if hazard is not None:
        print(f"explore leg skipped: route approaches {hazard.get('prefab')} guid={hazard.get('guid')}")
        return False
    hwnd = find_game_window()
    focus_game(hwnd)
    fresh = state
    fresh_position = log_position
    total_duration = 0.0
    route_changed = False
    light_lost = False
    pulses = 4 if navigation_mode == "road" else 1
    for _ in range(pulses):
        if night_travel_without_torch(fresh):
            light_lost = True
            break
        hazard = exploration_hazard_on_leg(fresh, target_x, target_z)
        if hazard is not None:
            print(f"explore leg stopped: route approaches {hazard.get('prefab')} guid={hazard.get('guid')}")
            return total_duration > 0
        current_position = fresh.get("player", {}).get("position", {})
        dx = target_x - float(current_position["x"])
        dz = target_z - float(current_position["z"])
        distance = math.hypot(dx, dz)
        if distance <= arrival_distance:
            break
        keys = movement_keys_for_delta(fresh, dx, dz)
        if not keys:
            raise RuntimeError("Cannot derive movement keys for exploration leg")
        if navigation_mode == "road":
            # Brake before narrow road waypoints; telemetry closes the loop.
            duration = min(0.5, max(0.08, full_leg_seconds * distance / 4.0 * 0.65))
        else:
            duration = full_leg_seconds * min(1.0, max(0.2, min(distance, leg_distance) / 4.0))
        try:
            tap(keys, duration)
        finally:
            release_movement_keys()
        total_duration += duration
        # A sample produced while the key was still held is not a post-move
        # position. Discard it and wait for the next telemetry record.
        _, after_release_position = latest_state(log_path)
        fresh, fresh_position = wait_for_fresh_state(
            log_path, max(fresh_position, after_release_position)
        )
        if night_travel_without_torch(fresh):
            light_lost = True
            break
        if navigation_mode == "road":
            updated_target = section(fresh).get("target") or {}
            updated_key = (
                round(float(updated_target.get("x", math.inf)), 2),
                round(float(updated_target.get("z", math.inf)), 2),
            )
            if updated_key != frontier_key:
                route_changed = True
                break
            if nearest_hostile(fresh, maximum_distance=6.0):
                break
    after = fresh.get("player", {}).get("position", {})
    if after.get("x") is None or after.get("z") is None:
        raise RuntimeError("Fresh telemetry has no player position after exploration")
    end_x = float(after["x"])
    end_z = float(after["z"])
    progress = remaining - math.hypot(target_x - end_x, target_z - end_z)
    EXPLORATION_STALL["last_position"] = (end_x, end_z)
    if light_lost:
        EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        print("explore leg stopped: night requires an equipped torch")
        return True
    if route_changed:
        EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        print(f"road route changed after movement; progress={progress:.2f} units")
        return True
    if progress < 0.15:
        EXPLORATION_STALL["count"] += 1
        print(f"frontier leg made insufficient progress ({progress:.2f} units); "
              f"consecutive stalls={EXPLORATION_STALL['count']}")
        current_target = section(fresh).get("target") or {}
        current_key = (
            round(float(current_target.get("x", math.inf)), 2),
            round(float(current_target.get("z", math.inf)), 2),
        )
        if EXPLORATION_STALL["count"] >= 2 and current_key == frontier_key:
            if navigation_mode == "graph":
                GRAPH_ROUTE_BLACKLIST[target_node_id or ""] = time.monotonic() + 30.0
                print(f"graph route to {target_node_id} paused for 30s after two stalled legs")
                EXPLORATION_STALL.update(target=None, count=0, last_position=None)
                return True
            tap([VK["reject_road" if navigation_mode == "road" else "reject_frontier"]], 0.08)
            wait_for_state_condition(
                log_path,
                fresh_position,
                lambda updated: (
                    section(updated).get("target") is None
                    or (
                        round(float(section(updated)["target"]["x"]), 2),
                        round(float(section(updated)["target"]["z"]), 2),
                    ) != frontier_key
                ),
                timeout=3.0,
            )
            print(f"frontier target {frontier_key} rejected after two stalled legs")
            EXPLORATION_STALL.update(target=None, count=0, last_position=None)
        return True
    EXPLORATION_STALL["count"] = 0
    print(
        f"frontier leg completed: target=({target_x:.2f},{target_z:.2f}) "
        f"planned={leg_distance:.2f} progress={progress:.2f} duration={total_duration:.2f}s"
    )
    return True


def follow_road(
    log_path: Path,
    target_x: float,
    target_z: float,
    leg_distance: float,
    full_leg_seconds: float = 1.0,
    max_distance: float = 10.0,
    max_legs: int = 8,
) -> bool:
    """Follow several live road waypoints in one bounded JEV action."""
    initial, _ = latest_state(log_path)
    initial_region = initial.get("navigation", {}).get("current_region")
    initial_biome = initial.get("navigation", {}).get("current_biome")
    initial_node = (initial.get("navigation", {}).get("graph") or {}).get("current_node")
    initial_phase = initial.get("world", {}).get("phase")
    seen_collectibles = {
        entity.get("guid") for entity in initial.get("nearby", [])
        if entity.get("guid") is not None
        and collect_target_available(entity)
    }
    distance_walked = 0.0
    completed_legs = 0
    new_resource_seen = False
    for index in range(max_legs):
        current, _ = latest_state(log_path)
        road = current.get("navigation", {}).get("road") or {}
        waypoint = road.get("target") or {}
        leg = road.get("leg") or {}
        if road.get("status") != "ready" or not waypoint or not leg:
            break
        if night_travel_without_torch(current) or nearest_hostile(current, maximum_distance=6.0):
            break
        if current.get("world", {}).get("phase") != initial_phase:
            break
        navigation = current.get("navigation", {})
        biome = navigation.get("current_biome")
        node = (navigation.get("graph") or {}).get("current_node")
        if initial_node is not None and node is not None:
            if node != initial_node:
                break
        elif initial_biome is not None and biome is not None:
            if biome != initial_biome:
                break
        elif initial_region is not None and navigation.get("current_region") != initial_region:
            break
        if index == 0 and (
            round(float(waypoint["x"]), 2), round(float(waypoint["z"]), 2)
        ) != (round(target_x, 2), round(target_z, 2)):
            print("road follow skipped: waypoint changed since JEV decision")
            return False
        if distance_walked + float(leg.get("distance") or leg_distance) > max_distance:
            break
        before = current.get("player", {}).get("position", {})
        moved = explore_leg(
            log_path,
            float(leg["x"]), float(leg["z"]), float(leg.get("distance") or leg_distance),
            full_leg_seconds,
            frontier_x=float(waypoint["x"]), frontier_z=float(waypoint["z"]),
            navigation_mode="road",
        )
        if not moved:
            break
        updated, _ = latest_state(log_path)
        after = updated.get("player", {}).get("position", {})
        progress = math.hypot(
            float(after.get("x", 0)) - float(before.get("x", 0)),
            float(after.get("z", 0)) - float(before.get("z", 0)),
        )
        if progress < 0.15:
            break
        distance_walked += progress
        completed_legs += 1
        if distance_walked >= max_distance:
            break
        new_resource_seen = new_resource_seen or any(
            entity.get("guid") is not None
            and entity["guid"] not in seen_collectibles
            and entity.get("prefab") in ROAD_INTEREST_PREFABS
            and float(entity.get("distance", math.inf)) <= 2.5
            and collect_target_available(entity)
            for entity in updated.get("nearby", [])
        )
        vitals = updated.get("player", {}).get("vitals", {})
        hunger = float(vitals.get("hunger") or 0)
        hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
        if hunger / hunger_max < 0.30 and safe_food_count(updated) > 0:
            break
        if new_resource_seen and distance_walked >= 4.0:
            break
    print(f"road follow completed: legs={completed_legs} distance={distance_walked:.2f}")
    return completed_legs > 0


def item_count(state: dict, prefab: str, equipped_only: bool = False) -> int:
    inventory = state.get("player", {}).get("inventory", {})
    groups = [inventory.get("equipped", [])]
    if not equipped_only:
        groups.append(inventory.get("items", []))
    return sum(
        int(item.get("count", 1))
        for group in groups
        for item in group
        if item.get("prefab") == prefab
    )


def is_weapon_item(item: dict) -> bool:
    if item.get("prefab") == "torch":
        return False
    return item.get("weapon") is True or item.get("prefab") in {
        "axe",
        "pickaxe",
        "spear",
        "spear_wathgrithr",
        "batbat",
        "tentaclespike",
        "hambat",
        "ruins_bat",
        "nightsword",
        "glasscutter",
    }


def has_weapon(state: dict, equipped_only: bool = False) -> bool:
    inventory = state.get("player", {}).get("inventory", {})
    groups = [inventory.get("equipped", [])]
    if not equipped_only:
        groups.append(inventory.get("items", []))
    return any(is_weapon_item(item) for group in groups for item in group)


def equip_weapon(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if has_weapon(state, equipped_only=True):
        print("equip already verified: weapon is equipped")
        return
    if not has_weapon(state):
        raise RuntimeError("No weapon is present in inventory; command not sent")
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["equip_weapon"]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: has_weapon(fresh, equipped_only=True),
    )
    equipped = verified.get("player", {}).get("inventory", {}).get("equipped", [])
    weapon = next((item for item in equipped if is_weapon_item(item)), {})
    print(f"equip verified: weapon={weapon.get('prefab')}")


def flee_from_hostile(
    log_path: Path,
    preferred_guid: Optional[int] = None,
    duration: float = 1,
    max_bursts: int = 8,
    clear_confirmations: int = 2,
) -> None:
    state, log_position = latest_state(log_path)
    hwnd = find_game_window()
    focus_game(hwnd)
    bursts = 0
    clear_count = 0
    blocked_keys: set[tuple[int, ...]] = set()
    try:
        while bursts < max_bursts:
            target = None
            if preferred_guid is not None:
                target = next(
                    (
                        entity
                        for entity in state.get("nearby", [])
                        if entity.get("guid") == preferred_guid
                        and entity.get("activeThreat") is True
                        and float(entity.get("distance", math.inf)) <= 12.0
                    ),
                    None,
                )
            if target is None:
                target = nearest_hostile(state, maximum_distance=12.0)

            if target is None:
                clear_count += 1
                if clear_count >= clear_confirmations:
                    print(f"flee verified clear after {bursts} burst(s)")
                    return
                state, log_position = wait_for_fresh_state(log_path, log_position)
                continue

            clear_count = 0
            selected = choose_escape_route(state, blocked_keys)
            if selected is None:
                raise RuntimeError("No passable, unblocked escape direction; stopped instead of running into the boundary")
            route, keys, predicted_gain = selected
            safe_distance = float(route["distance"])
            pulse = min(duration, max(0.18, (safe_distance - 0.5) / 5.0))
            before = state.get("player", {}).get("position", {})
            tap(keys, pulse)
            bursts += 1
            print(
                f"flee burst={bursts}/{max_bursts} hostile={target.get('prefab')} "
                f"guid={target.get('guid')} distance={float(target.get('distance', 0)):.2f} "
                f"safe_path={safe_distance:.2f} predicted_gain={predicted_gain:.2f}"
            )
            state, log_position = wait_for_fresh_state(log_path, log_position)
            after = state.get("player", {}).get("position", {})
            progress = math.hypot(
                float(after.get("x", 0)) - float(before.get("x", 0)),
                float(after.get("z", 0)) - float(before.get("z", 0)),
            )
            if progress < 0.3:
                blocked_keys.add(tuple(keys))
                print(f"flee direction blocked: keys={keys} progress={progress:.2f}; rerouting")
    finally:
        release_movement_keys()
    remaining = nearest_hostile(state, maximum_distance=12.0)
    if remaining is None:
        print(f"flee completed after {bursts} burst(s); no hostile remains in range")
    else:
        print(
            f"flee reached safety limit after {bursts} burst(s); hostile still at "
            f"{float(remaining.get('distance', 0)):.2f} units"
        )


def craft_torch(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if not state.get("crafting", {}).get("torch", False):
        raise RuntimeError("Torch is not currently craftable; command not sent")
    before = item_count(state, "torch")
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["craft_torch"]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: item_count(fresh, "torch") > before,
    )
    print(f"craft verified: torch count {before} -> {item_count(verified, 'torch')}")
    if (
        verified.get("world", {}).get("phase") != "night"
        and item_count(verified, "torch", equipped_only=True) > 0
    ):
        # DST may auto-equip a newly crafted torch. Preserve fuel before night.
        unequip_torch(log_path, daylight_only=True)


def equip_torch(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if state.get("world", {}).get("phase") != "night":
        raise RuntimeError("Torch equip is allowed only at night; command not sent")
    if has_nearby_lit_fire(state):
        raise RuntimeError("A lit campfire or firepit is nearby; torch equip not needed")
    if item_count(state, "torch") <= 0:
        raise RuntimeError("No torch is present in inventory; command not sent")
    if item_count(state, "torch", equipped_only=True) > 0:
        print("equip already verified: torch is equipped")
        return
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["equip_torch"]], 0.08)
    wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: item_count(fresh, "torch", equipped_only=True) > 0,
    )
    print("equip verified: torch is equipped")


def unequip_torch(log_path: Path, daylight_only: bool = False) -> None:
    state, log_position = latest_state(log_path)
    if daylight_only and state.get("world", {}).get("phase") == "night":
        print("craft cleanup skipped: night began before torch could be unequipped")
        return
    if state.get("world", {}).get("phase") == "night" and not has_nearby_lit_fire(state):
        raise RuntimeError("Cannot unequip the only light source at night; command not sent")
    if item_count(state, "torch", equipped_only=True) <= 0:
        if daylight_only:
            print("craft cleanup skipped: torch is already unequipped")
            return
        raise RuntimeError("A torch is not equipped in the hand slot; command not sent")
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["unequip_torch"]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: (
            item_count(fresh, "torch", equipped_only=True) == 0
            and item_count(fresh, "torch") > 0
        ),
    )
    print(f"unequip verified: {item_count(verified, 'torch')} torch in inventory")


def craft_inventory_item(log_path: Path, prefab: str, crafting_key: str) -> None:
    state, log_position = latest_state(log_path)
    if not state.get("crafting", {}).get(prefab, False):
        raise RuntimeError(f"{prefab} is not currently craftable; command not sent")
    before = item_count(state, prefab)
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK[crafting_key]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: item_count(fresh, prefab) > before,
    )
    print(f"craft verified: {prefab} count {before} -> {item_count(verified, prefab)}")


def nearby_count(state: dict, prefabs=(), required_tag: Optional[str] = None) -> int:
    return sum(
        1
        for entity in state.get("nearby", [])
        if (not prefabs or entity.get("prefab") in prefabs)
        and (required_tag is None or required_tag in entity.get("tags", []))
    )


def build_campfire(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if state.get("world", {}).get("phase") != "night":
        raise RuntimeError("Campfire construction is allowed only at night; command not sent")
    if has_nearby_lit_fire(state):
        raise RuntimeError("A lit campfire or firepit is already nearby; command not sent")
    if not state.get("crafting", {}).get("campfire", False):
        raise RuntimeError("Campfire is not currently craftable; command not sent")
    before = nearby_count(state, ("campfire",))
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["build_campfire"]], 0.08)
    wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: nearby_count(fresh, ("campfire",)) > before,
        timeout=15.0,
    )
    print("build verified: campfire appeared nearby")


def build_sciencemachine(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if not state.get("crafting", {}).get("sciencemachine", False):
        raise RuntimeError("Science machine is not currently craftable; command not sent")
    before = nearby_count(state, ("sciencemachine",))
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["build_sciencemachine"]], 0.08)
    wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: nearby_count(fresh, ("sciencemachine",)) > before,
        timeout=15.0,
    )
    print("build verified: science machine appeared nearby")


SAFE_FOOD_PREFABS = ("cookedmeat", "cookedsmallmeat", "berries_cooked", "carrot_cooked", "berries", "carrot", "seeds_cooked", "seeds")
RAW_COOKABLE_FOOD_PREFABS = ("meat", "smallmeat", "berries", "carrot")
COOKED_FOOD_PREFABS = ("cookedmeat", "cookedsmallmeat", "berries_cooked", "carrot_cooked")


def safe_food_count(state: dict) -> int:
    return sum(item_count(state, prefab) for prefab in SAFE_FOOD_PREFABS)


def has_nearby_cooker(state: dict, maximum_distance: float = 6.0) -> bool:
    return any(
        entity.get("prefab") in {"campfire", "firepit"}
        and {"fire", "cooker"}.issubset(set(entity.get("tags", [])))
        and float(entity.get("distance", math.inf)) <= maximum_distance
        for entity in state.get("nearby", [])
    )


def eat_safe_food(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    cooked_count = sum(item_count(state, prefab) for prefab in COOKED_FOOD_PREFABS)
    raw_count = sum(item_count(state, prefab) for prefab in RAW_COOKABLE_FOOD_PREFABS)
    if cooked_count == 0 and raw_count > 0 and has_nearby_cooker(state):
        try:
            cook_food(log_path)
        except RuntimeError:
            state, log_position = latest_state(log_path)
            if safe_food_count(state) <= 0:
                raise
            print("cook failed; eating available safe food instead")
        state, log_position = latest_state(log_path)
    before_food = safe_food_count(state)
    before_hunger = float(state.get("player", {}).get("vitals", {}).get("hunger", 0))
    if before_food <= 0:
        raise RuntimeError("No whitelisted safe food is available; command not sent")
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["eat_safe_food"]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: (
            safe_food_count(fresh) < before_food
            or float(fresh.get("player", {}).get("vitals", {}).get("hunger", 0)) > before_hunger
        ),
    )
    after_hunger = verified.get("player", {}).get("vitals", {}).get("hunger")
    print(f"eat verified: hunger {before_hunger} -> {after_hunger}")


def cook_food(log_path: Path) -> None:
    state, log_position = latest_state(log_path)
    if sum(item_count(state, prefab) for prefab in RAW_COOKABLE_FOOD_PREFABS) <= 0:
        raise RuntimeError("No whitelisted raw food is available; cook command not sent")
    if not has_nearby_cooker(state):
        raise RuntimeError("No nearby lit cooker is available; cook command not sent")
    before = sum(item_count(state, prefab) for prefab in COOKED_FOOD_PREFABS)
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["cook_food"]], 0.08)
    verified, _ = wait_for_state_condition(
        log_path,
        log_position,
        lambda fresh: sum(item_count(fresh, prefab) for prefab in COOKED_FOOD_PREFABS) > before,
        timeout=10.0,
    )
    after = sum(item_count(verified, prefab) for prefab in COOKED_FOOD_PREFABS)
    print(f"cook verified: cooked food {before} -> {after}")


def trigger_semantic_action(log_path: Path, action_key: str) -> None:
    _state, log_position = latest_state(log_path)
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK[action_key]], 0.08)
    wait_for_fresh_state(log_path, log_position)
    print(f"semantic action sent: {action_key}")


def force_attack(log_path: Path) -> None:
    """Use DST's native Ctrl+F command and let the game select the target."""
    _state, log_position = latest_state(log_path)
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK["ctrl"], VK["attack"]], 0.08)
    wait_for_fresh_state(log_path, log_position)
    print("native force attack sent: Ctrl+F")


def _conservative_tool_uses(state: dict, prefab: str, maximum: int) -> int:
    candidates = []
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
            candidates.append(min(matching))
    return max(candidates, default=0)


def complete_work_action(log_path: Path, action_key: str, target_tag: str) -> None:
    state, log_position = latest_state(log_path)
    preexisting_drop_guids = {
        entity["guid"] for entity in state.get("nearby", [])
        if entity.get("guid") is not None and "_inventoryitem" in entity.get("tags", [])
    }
    tool_prefab, maximum = (
        ("axe", 100) if target_tag == "CHOP_workable" else ("pickaxe", 33)
    )
    available = _conservative_tool_uses(state, tool_prefab, maximum)
    candidates = [
        entity for entity in state.get("nearby", [])
        if target_tag in entity.get("tags", [])
        and float(entity.get("distance", math.inf)) <= 6
        and entity.get("work_required") is not None
        and available >= int(entity["work_required"])
    ]
    if not candidates:
        raise RuntimeError(f"No nearby {target_tag} target; command not sent")
    previous_sequence = int((state.get("work") or {}).get("sequence") or 0)
    hwnd = find_game_window()
    focus_game(hwnd)
    tap([VK[action_key]], 0.08)

    expected_action = "chop" if target_tag == "CHOP_workable" else "mine"
    acknowledged, ack_position = wait_for_state_condition(
        log_path, log_position,
        lambda fresh: (
            int((fresh.get("work") or {}).get("sequence") or 0) > previous_sequence
            and (fresh.get("work") or {}).get("action") == expected_action
        ),
        timeout=6.0,
    )
    work = acknowledged["work"]
    sequence = int(work["sequence"])
    guid = work["target_guid"]
    origin = (float(work["x"]), float(work["z"]))
    if work.get("status") not in {"completed", "failed"}:
        verified, verified_position = wait_for_state_condition(
            log_path, ack_position,
            lambda fresh: (
                int((fresh.get("work") or {}).get("sequence") or 0) == sequence
                and (fresh.get("work") or {}).get("status") in {"completed", "failed"}
            ),
            timeout=35.0,
        )
        work = verified["work"]
    else:
        verified, verified_position = acknowledged, ack_position
    if work.get("status") != "completed":
        raise RuntimeError(f"{expected_action} failed: {work.get('reason', 'unknown reason')}")
    print(f"work verified complete: {action_key} target={guid}")
    collect_nearby_drops(
        log_path, WORK_DROP_PREFABS[target_tag], origin, verified, verified_position,
        preexisting_drop_guids=preexisting_drop_guids,
    )


def collect(
    log_path: Path,
    prefab: str,
    max_steps: int = 16,
    preferred_guid: Optional[int] = None,
    strict_preferred: bool = False,
) -> bool:
    state, log_position = latest_state(log_path)
    hwnd = find_game_window()
    focus_game(hwnd)

    # The JEV decision may take long enough for a source to become empty.
    # Require a telemetry record newer than that decision before interacting.
    state, log_position = wait_for_fresh_state(log_path, log_position)
    threat = nearest_hostile(state, maximum_distance=5.0)
    if threat is not None:
        print(f"collection interrupted: hostile={threat.get('prefab')} distance={float(threat.get('distance', 0)):.2f}")
        return False
    target = find_collectible_target(state, prefab, preferred_guid)
    if target is None:
        raise RuntimeError(f"No currently collectible target with prefab '{prefab}'; command not sent")
    if strict_preferred and target.get("guid") != preferred_guid:
        print(f"collection skipped: preferred {prefab} target is no longer available")
        return False
    guid = target["guid"]
    original_target = target
    previous_distance = float(target["distance"])
    worse_steps = 0
    product = COLLECT_PRODUCTS.get(prefab)
    product_count_before = item_count(state, product) if product is not None else 0

    print(f"target={prefab} guid={guid} distance={previous_distance:.2f}")
    try:
        for step in range(1, max_steps + 1):
            if night_travel_without_torch(state):
                print("collection interrupted: night requires an equipped torch, even beside a lit fire")
                return False
            threat = nearest_hostile(state, maximum_distance=5.0)
            if threat is not None:
                print(f"collection interrupted: hostile={threat.get('prefab')} distance={float(threat.get('distance', 0)):.2f}")
                return False
            target = find_target(state, prefab, guid)
            if collect_target_completed(original_target, target):
                print("collection verified: target is no longer collectible")
                return True
            distance = float(target["distance"])
            if distance <= 1.0:
                if not collect_target_available(target):
                    raise RuntimeError(f"Target '{prefab}' is no longer pickable; interaction not sent")
                tap([VK["interact"]], 0.08)
                state, log_position = wait_for_fresh_state(log_path, log_position)
                if product is not None and item_count(state, product) > product_count_before:
                    print(f"interaction verified: inventory gained {product}")
                    return True
                remaining = find_target(state, prefab, guid)
                if collect_target_completed(original_target, remaining):
                    print("interaction verified: target is no longer collectible")
                    return True
                else:
                    print(f"interaction did not complete; retrying at {float(remaining['distance']):.2f}")
                    continue

            keys = movement_keys_for_target(state, target)
            duration = approach_step_duration(distance)
            tap(keys, duration)
            state, log_position = wait_for_fresh_state(log_path, log_position)
            updated = find_target(state, prefab, guid)
            if collect_target_completed(original_target, updated):
                print("collection completed while approaching")
                return True
            new_distance = float(updated["distance"])
            print(f"step={step} distance={new_distance:.2f}")
            if new_distance >= previous_distance - 0.03:
                worse_steps += 1
            else:
                worse_steps = 0
            if worse_steps >= 3:
                raise RuntimeError("Approach is not converging; stopped safely")
            previous_distance = new_distance
        raise RuntimeError("Maximum approach steps reached; stopped safely")
    finally:
        release_movement_keys()


def collect_nearby_drops(
    log_path: Path,
    expected_prefabs: frozenset[str],
    origin: tuple[float, float],
    state: dict,
    log_position: int,
    radius: float = 4.0,
    max_stacks: int = 12,
    preexisting_drop_guids: Optional[set[int]] = None,
) -> int:
    """Collect known work products and newly spawned loose drops near the target."""
    gathered = 0
    attempted: set[int] = set()
    waited_for_drops = False
    preexisting_drop_guids = preexisting_drop_guids or set()
    while gathered < max_stacks:
        if night_travel_without_torch(state) or nearest_hostile(state, maximum_distance=5.0):
            break
        position = state.get("player", {}).get("position", {})
        px, pz = float(position.get("x", 0)), float(position.get("z", 0))
        candidates = [
            entity for entity in state.get("nearby", [])
            if entity.get("guid") is not None
            and entity["guid"] not in attempted
            and "_inventoryitem" in entity.get("tags", [])
            and (entity.get("prefab") in expected_prefabs
                 or entity["guid"] not in preexisting_drop_guids)
            and math.hypot(
                px + float(entity.get("dx", 0)) - origin[0],
                pz + float(entity.get("dz", 0)) - origin[1],
            ) <= radius
        ]
        if not candidates:
            if not waited_for_drops:
                waited_for_drops = True
                try:
                    state, log_position = wait_for_fresh_state(log_path, log_position, timeout=1.5)
                except RuntimeError:
                    break
                continue
            break
        target = min(candidates, key=lambda entity: float(entity.get("distance", math.inf)))
        guid = target["guid"]
        prefab = target["prefab"]
        attempted.add(guid)
        try:
            collected = collect(
                log_path, prefab, max_steps=8, preferred_guid=guid, strict_preferred=True,
            )
        except RuntimeError as exc:
            print(f"work drop skipped: prefab={prefab} guid={guid} reason={exc}")
            collected = False
        if collected:
            gathered += 1
        state, log_position = latest_state(log_path)
    print(f"work drops collected: stacks={gathered}")
    return gathered


def run_self_test() -> int:
    state = {
        "camera": {"right": {"x": 1, "z": 0}, "forward": {"x": 0, "z": 1}},
        "nearby": [
            {"guid": 1, "prefab": "grass", "distance": 4, "dx": 0, "dz": 4},
            {"guid": 2, "prefab": "grass", "distance": 2, "dx": -2, "dz": 0},
        ],
    }
    assert find_target(state, "grass")["guid"] == 2
    assert movement_keys_for_target(state, state["nearby"][0]) == [VK["up"]]
    assert movement_keys_for_target(state, state["nearby"][1]) == [VK["left"]]
    assert approach_step_duration(8.0) == 1.0
    assert approach_step_duration(4.0) > 0.70
    assert approach_step_duration(1.1) == 0.18
    original_pickable = {
        "guid": 3,
        "prefab": "grass",
        "tags": ["pickable", "DIG_workable"],
        "pickable": True,
    }
    assert collect_target_available(original_pickable)
    target_state = {
        "nearby": [
            {"guid": 8, "prefab": "grass", "distance": 0.5, "tags": [], "pickable": False},
            {"guid": 9, "prefab": "grass", "distance": 2.0, "tags": ["pickable"], "pickable": True},
        ]
    }
    assert find_collectible_target(target_state, "grass")["guid"] == 9
    assert find_collectible_target(target_state, "grass", preferred_guid=9)["guid"] == 9
    assert find_collectible_target(target_state, "grass", preferred_guid=8)["guid"] == 9
    assert collect_target_completed(
        original_pickable,
        {"guid": 3, "prefab": "grass", "tags": ["DIG_workable"], "pickable": False},
    )
    assert not collect_target_completed(original_pickable, original_pickable)
    original_loose = {"guid": 4, "tags": ["_inventoryitem"]}
    assert collect_target_completed(original_loose, None)
    print("self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--self-test", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status")
    move_parser = subparsers.add_parser("move")
    move_parser.add_argument("direction", choices=("up", "down", "left", "right"))
    move_parser.add_argument("--seconds", type=float, default=0.25)
    subparsers.add_parser("interact")
    subparsers.add_parser("craft-torch")
    subparsers.add_parser("equip-torch")
    subparsers.add_parser("unequip-torch")
    subparsers.add_parser("craft-axe")
    subparsers.add_parser("craft-pickaxe")
    subparsers.add_parser("build-campfire")
    subparsers.add_parser("eat")
    subparsers.add_parser("cook")
    subparsers.add_parser("chop")
    subparsers.add_parser("mine")
    subparsers.add_parser("attack")
    subparsers.add_parser("equip-weapon")
    subparsers.add_parser("flee")
    subparsers.add_parser("stop")
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("prefab")
    collect_parser.add_argument("--max-steps", type=int, default=16)
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.command == "status":
        state, _ = latest_state(args.log)
        print(status_text(state))
        return 0
    if args.command == "stop":
        release_movement_keys()
        print("movement keys released")
        return 0
    if args.command == "collect":
        collect(args.log, args.prefab, max_steps=max(1, min(args.max_steps, 40)))
        return 0
    if args.command == "craft-torch":
        craft_torch(args.log)
        return 0
    if args.command == "equip-torch":
        equip_torch(args.log)
        return 0
    if args.command == "unequip-torch":
        unequip_torch(args.log)
        return 0
    if args.command == "craft-axe":
        craft_inventory_item(args.log, "axe", "craft_axe")
        return 0
    if args.command == "craft-pickaxe":
        craft_inventory_item(args.log, "pickaxe", "craft_pickaxe")
        return 0
    if args.command == "build-campfire":
        build_campfire(args.log)
        return 0
    if args.command == "eat":
        eat_safe_food(args.log)
        return 0
    if args.command == "cook":
        cook_food(args.log)
        return 0
    if args.command == "chop":
        complete_work_action(args.log, "chop_nearest_tree", "CHOP_workable")
        return 0
    if args.command == "mine":
        complete_work_action(args.log, "mine_nearest_rock", "MINE_workable")
        return 0
    if args.command == "attack":
        force_attack(args.log)
        return 0
    if args.command == "equip-weapon":
        equip_weapon(args.log)
        return 0
    if args.command == "flee":
        flee_from_hostile(args.log)
        return 0

    hwnd = find_game_window()
    focus_game(hwnd)
    try:
        if args.command == "move":
            tap([VK[args.direction]], args.seconds)
        elif args.command == "interact":
            tap([VK["interact"]], 0.08)
        else:
            parser.error("choose a command or use --self-test")
    finally:
        release_movement_keys()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
