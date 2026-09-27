# JEV DST Agent 1.3.1 — telemetry, quarter-day planning and fast JEV decisions

This package connects JEV decisions to a bounded Don't Starve Together client Mod.
It exports game state and can execute only the actions explicitly listed below.

## What it exports

Once per second, the client mod writes one JSON record to `client_log.txt` with:

- day, phase and world time;
- health, hunger and sanity;
- inventory and equipped items;
- whether a torch or campfire can be crafted;
- nearby useful items, resources, light sources and threats.
- observed region and road state, persistent targets and current legs.

Short records start with `[JEV_DST_STATE]`. Records longer than the safe game-log
line size are emitted as numbered `[JEV_DST_CHUNK]` lines and reassembled by
Python. A trailing tab added by the game log is removed from each part first.
The controller and watcher accept only complete, valid JSON records;
truncated lines left by older Mod versions are ignored.

## Install the mod

Copy the entire `jev_dst_agent` folder into DST's `mods` directory. On this PC,
the detected destination is:

```text
C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods\jev_dst_agent
```

Start DST, open **Mods**, enable **JEV DST Agent**, apply the changes, and enter a
local/private world.

## Watch live state

Open PowerShell in this folder and run:

```powershell
C:\Users\16425\anaconda3\python.exe .\watch_state.py
```

For the complete JSON records:

```powershell
C:\Users\16425\anaconda3\python.exe .\watch_state.py --full
```

## Bounded controls

`controller.py` supports movement, collection, crafting, work, eating, cooking and combat operations:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py status
C:\Users\16425\anaconda3\python.exe .\controller.py move up --seconds 0.25
C:\Users\16425\anaconda3\python.exe .\controller.py interact
C:\Users\16425\anaconda3\python.exe .\controller.py collect grass
C:\Users\16425\anaconda3\python.exe .\controller.py cook
```

`collect` selects the nearest matching observed prefab, approaches it through
short WASD pulses, waits for fresh telemetry after every pulse, then presses the
ordinary action key. `stop` releases every movement key if a run is interrupted.
JEV's safe loose-resource candidates include flint, logs and rocks.
Collection approaches to within one world unit before pressing Space. Success is
verified when a source loses its `pickable` tag or a loose item disappears; if
the interaction does not complete, the controller retries instead of advancing
the JEV loop.

Grass, saplings and berry bushes expose an explicit live `pickable` value and
are omitted from JEV's candidates while empty. Immediately before Space, the
controller waits for a newer state and checks the same value again. A matching
inventory gain (cut grass, twigs or berries) also proves that collection has
finished, even though the harvested plant entity remains in the world.
The chosen collectible's GUID is carried into execution, so a nearer depleted
plant cannot replace the live target. If that GUID becomes unavailable before
execution, the controller falls back to the nearest currently collectible plant
of the same prefab.

## Offline automatic tests

Run the complete regression suite without launching DST, calling JEV or sending
any keyboard input:

```powershell
C:\Users\16425\anaconda3\python.exe .\run_tests.py
```

The suite uses simulated telemetry and mocked game input. It covers depleted
versus live resource selection, GUID handoff, fallback selection, and collection
completion when inventory increases while the harvested plant remains visible.

Collection movement uses distance-scaled pulses: up to one second while far
away, braking to 0.18 seconds near interaction range. An active pursuer within
five world units interrupts collection immediately. When pursued at close range,
an unarmed character receives only a flee action; a carried weapon can be
equipped, and attack is exposed only while a weapon is already equipped. Fleeing
remains available during night and critical hunger. Only entities marked
`activeThreat` expose fleeing. A flee action is a bounded sequence: after every
one-second movement burst the controller reads fresh telemetry and recomputes
the direction away from the pursuer. It stops after two consecutive clear
observations, or after eight bursts as a safety limit.

Light management is phase-driven: the equip action is hidden during day and
dusk, while unequipping remains one option among JEV's other actions. At night,
a torch is equipped only when no lit campfire or firepit is within eight world
units. Campfires can be built only at night; while beside a lit campfire or
firepit, the equip action is hidden and unequipping remains available without
forcing it. Movement away from light while unequipped remains blocked unless
fleeing an immediate hostile.

Torch actions are semantic Mod operations with post-action verification:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py craft-torch
C:\Users\16425\anaconda3\python.exe .\controller.py equip-torch
C:\Users\16425\anaconda3\python.exe .\controller.py unequip-torch
```

The JEV candidate list contains `craft_torch` only while telemetry reports that
the recipe is craftable and no torch is already held. `equip_torch` appears only
at night while a torch exists, is not equipped, and no lit campfire or firepit
is nearby. `unequip_torch` appears during day or dusk while a torch is equipped,
and at night when a lit campfire or firepit is nearby.
After `craft_torch` succeeds, the controller checks whether DST auto-equipped the
new torch. If it is still day or dusk, it unequips the torch within the same
action to save fuel; if night has begun, it leaves the torch equipped. JEV need
not choose a separate unequip action for this cleanup.

Additional bounded actions:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py craft-axe
C:\Users\16425\anaconda3\python.exe .\controller.py craft-pickaxe
C:\Users\16425\anaconda3\python.exe .\controller.py chop
C:\Users\16425\anaconda3\python.exe .\controller.py mine
C:\Users\16425\anaconda3\python.exe .\controller.py eat
C:\Users\16425\anaconda3\python.exe .\controller.py cook
C:\Users\16425\anaconda3\python.exe .\controller.py build-campfire
C:\Users\16425\anaconda3\python.exe .\controller.py attack
C:\Users\16425\anaconda3\python.exe .\controller.py equip-weapon
C:\Users\16425\anaconda3\python.exe .\controller.py flee
```

Attack is exposed only when telemetry sees a valid hostile/monster target and a
weapon is already equipped, then sends DST's native `Ctrl+F` command; it
does not depend on mouse coordinates or the Mod's F10 bridge. Campfire construction follows DST's
native two-stage flow: buffer/craft the recipe, then place it. Chop and
mine now continue until the chosen target is completely harvested. Their JEV
actions are exposed only when telemetry proves that a matching basic tool has
enough remaining durability for the full job: normal trees are budgeted
conservatively at 15 chops; common rocks use their game-defined 6/4/2 strikes.
After a target is fully worked, the controller picks up up to twelve nearby
drop stacks within four units: known products such as logs, pinecones, acorns,
rocks, flint, nitre, and gold, plus other loose items newly seen after this
work action. Unrelated items already there are not automatically collected.
The Mod reports its actual
target GUID, position, sequence, and completion status in telemetry; the
controller no longer selects a separate target for verification. It stops
gathering for a nearby pursuer or unsafe night travel.
Eating uses a conservative early-game food whitelist, including cooked meat
and cooked morsels produced by the cooking action.

`cook_food` is exposed only when a lit campfire or firepit is within six units
and the inventory contains raw meat, morsels, berries, or carrots. The Mod
rechecks the `cookable` tag and invokes the game's native `COOK` action for one
item. The controller verifies that a cooked food item appeared in inventory.
Already-cooked and hazardous ingredients are not included.

When JEV chooses `eat_safe_food`, existing cooked food is eaten directly. If
only raw cookable food is carried and a lit cooker is within six units, the
controller cooks one item, verifies the cooked result, and then eats one item.
If cooking fails but safe raw berries or carrots remain, it eats those rather
than repeatedly waiting to cook. Raw meat is not used as a fallback.

## Candidate-pruning principle

Candidate pruning does not choose an action on JEV's behalf. Its purpose is to
remove actions that are unavailable, contextually meaningless, or excluded by
an explicit safety constraint. JEV still ranks and selects among all remaining
meaningful actions.

For example, an equipped torch during day or dusk exposes `unequip_torch`
because putting it away preserves fuel, while `equip_torch` is omitted when no
torch is equipped because carrying a lit torch in daylight provides no useful
benefit. At night the relationship reverses when there is no other light source;
beside a lit campfire or firepit, equipping a torch is again omitted as
redundant. Similarly, depleted plants, unavailable recipes, insufficiently
durable tools, and exploration legs rejected by the world map API are not shown as
candidates. The pruning layer determines which operations currently make
sense; it does not decide which sensible operation is best.

## JEV decision

JEV makes fast next-action decisions; it does not generate the daily plan.
With `DEEPSEEK_API_KEY` in `.env`, the `--loop --execute` controller requests a short DeepSeek plan
in the background for each quarter of a game day, using normalized `world.time`
windows 0–25%, 25–50%, 50–75%, and 75–100%. Starting mid-day requests only the
current window. The default model
is `deepseek-flash`; `DEEPSEEK_MODEL` and `DEEPSEEK_API_URL` are optional overrides.
The planner receives current telemetry, up to 40 recent action outcomes from
this Python run, and the local [DSTKnowledge.txt](DSTKnowledge.txt) reference.
The reference contains common recipes checked against the installed game's
recipe script; it is reread for every plan and can be edited. The repeating
per-second game log is not sent. Its plan is passed as
advisory `daily_plan` state once ready. JEV continues deciding while planning
is in progress; live facts, available actions, and hard safety rules take
precedence. Two consecutive failures of the same action or successfully building
a science machine trigger a same-window replan, at most once per 60 seconds.
Length truncation or empty JSON may trigger one bounded background retry;
if planning still fails, JEV continues without a plan and tries again the next quarter.
Previous-quarter plans are not passed to JEV, and late replies are discarded.
History is in memory only and resets when the controller restarts.
The terminal shows the day, quarter, loaded knowledge length, DeepSeek request
time and the first 240 characters of
the validated plan JSON; longer previews end with an ellipsis. Retries and
final errors show the API finish reason when available and the request time without logging the
raw response or API key.

The JEV prompt separates hard constraints from decision guidance and describes
each candidate as one complete bounded action. Chopping/mining include nearby
drop pickup; `eat_safe_food` can cook and then eat; `cook_food` alone prepares
food for later. The state includes hunger fraction, safe-food count and nearby
cooker availability. Guidance does not impose a fixed action ranking or choose
on JEV's behalf.

Put the TypeSafe key in `.env` (see `.env.example`). A decision is dry-run by
default:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --show-probabilities
```

Add `--execute` only after reviewing a dry run:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --execute
```

For continuous autonomous control, use:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --loop --execute
```

The loop has no cooldown by default and senses again immediately after each
completed action. Use `--interval 1` or another value up to 60 seconds to add a
cooldown. Errors still use a minimum one-second exponential retry delay to avoid
a tight failure loop.
Long actions such as chopping, mining, collecting and building finish before the
next cycle begins, so actions never overlap. Day 2 is not a terminal state: the
loop continues indefinitely and stops only on death or when interrupted with
`Ctrl+C`. Transient errors retry with bounded exponential backoff.

Exploration offers two semantic actions instead of four directions:
`explore_current_region` surveys the current ground-tile biome, while
`explore_other_region` follows an observed road toward another biome.
The road action is omitted when no reachable, unexplored road continuation is
known. A road is a travel cue, not proof that a new region is ahead.

The Mod samples ground tiles, topology IDs, roads and passability only around
the player; it does not read the entire unexplored world map. Topology remains
available as world-generation context, not as a biome label. `current_biome`
classifies forest, grassland, savanna, swamp, rocky ground, deciduous forest,
desert and dirt from actual ground tiles. On roads or artificial flooring it
samples adjacent natural ground; a biome change needs two consecutive samples.
`biomes_seen` lists observed terrain types. Spawn and mixed areas cannot be
identified from a single tile; desert ground alone cannot distinguish an oasis.
Observed road segments form a small graph: the route may backtrack over
visited segments to reach an unvisited branch, but visited segments alone do
not create an exploration action.

For current-region exploration, the Mod divides nearby terrain into four-unit
cells and remembers observed cells. A frontier is a known passable cell in the
current ground-tile biome adjacent to at least one unknown cell. Its baseline score is:

```text
score = information_gain * 10 - distance * 0.5 - visits * 6
```

The highest-scoring reachable frontier becomes a persistent current-region target. The target
survives intervening collection, crafting and other decisions until it is
reached, the biome changes, or its next path segment becomes invalid. An invalid target is
blacklisted for 30 seconds before another frontier is selected.

Current-region exploration walks only the next leg of at most four units.
One road-exploration decision follows successive live waypoints for at most
eight legs and ten units, instead of asking JEV again after each roughly
two-unit waypoint. Each road leg uses up to four short key pulses, with fresh
telemetry after every release. Arrival allows 1.5 units for a region target and
1.25 units for a road waypoint, avoiding reverse corrections just to hit an
exact coordinate. The
Mod samples each leg every 0.75 units using `TheWorld.Map:IsPassableAtPoint`.
The controller stops early for a nearby newly discovered resource after at
least four units, a hostile, a biome or phase change, critical hunger with
carried food, lost night lighting, or the end of the road. JEV then chooses
again. Direction keys are only an execution detail derived from the target
vector. Adjust the duration of a full four-unit leg with `--move-seconds`;
the default is one second and accepted values are 0.2–2.0 seconds.

The controller checks actual position after each leg. If the same target
produces less than 0.15 units of progress twice in succession, it asks the Mod
to blacklist that frontier or road waypoint for 30 seconds. JEV chooses the
next action after each current-region leg or bounded road-follow action. If
the player moves during a JEV call,
the controller follows the Mod's latest passable leg for the same frontier.

Equipping a torch uses the same inventory-tile action as manually clicking the
torch, rather than the action-system auto-equip helper.

Safe resource collection uses a `0.25` confidence threshold. The JEV prompt also
explicitly prefers unequipping a torch during day and dusk to preserve its
durability.

Below 30% hunger with safe food already carried, waiting, exploration and
optional gathering are omitted as dominated choices. JEV still selects from
the remaining meaningful actions: eating, and escape or urgent night lighting
when applicable. Emergency eating below 30% hunger uses a `0.00` threshold;
ordinary eating remains at `0.25`;
an explicit `--min-confidence` override still
takes precedence. Without carried food, exploration remains available to seek it.

Execution uses these default confidence thresholds: `0.00` for fleeing,
waiting, and emergency eating, `0.15` for either exploration action, `0.45`
for attacking, and `0.25`
for every other action, including collection, cooking, eating, crafting, and
building. Pass
`--min-confidence` to override these values globally. Exploration and collection
without an equipped torch at night are blocked even beside a lit campfire: a
stationary fire does not illuminate the full route. The controller rechecks
during movement, stopping if night falls mid-route. Fleeing from an immediate
hostile remains permitted. Attack is unavailable while unarmed. `.env` is ignored by Git and
deliberately excluded from release archives.
