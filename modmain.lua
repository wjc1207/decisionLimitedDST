local json = require("json")
local G = GLOBAL
local protected_call = G.pcall

local LOG_PREFIX = "[JEV_DST_STATE]"
local CHUNK_PREFIX = "[JEV_DST_CHUNK]"
local MAX_LOG_PAYLOAD_BYTES = 3000
local telemetry_sequence = 0
local SAMPLE_INTERVAL = GetModConfigData("sample_interval") or 1.0
local SCAN_RADIUS = GetModConfigData("scan_radius") or 12
local MAX_NEARBY = 40
local PLAYER_WAIT_INTERVAL = 0.5
local NAV_CELL_SIZE = 4
local NAV_OBSERVE_RADIUS = SCAN_RADIUS
local NAV_LEG_DISTANCE = 4
local NAV_PROBE_STEP = 0.75
local NAV_ARRIVAL_DISTANCE = 1.5
local ROAD_ARRIVAL_DISTANCE = 1.25
local NAV_BLACKLIST_SECONDS = 30
local HAZARD_MEMORY_SECONDS = 90
local HAZARD_CLEARANCE = 14
local HAZARD_PENALTY_RANGE = 26
local ESCAPE_PROBE_DISTANCE = 6
local ROAD_CELL_SIZE = 2
local ROAD_SCAN_RADIUS = 8
local ROAD_SCAN_BUDGET = 20

-- A topology node is a world-generation branch, not a biome. Classify only
-- ground tiles whose meaning is unambiguous; roads and player-laid floors do
-- not create a new biome. Spawn and mixed areas need context, not a tile ID.
local BIOME_BY_TILE =
{
    [G.GROUND.FOREST] = "forest",
    [G.GROUND.GRASS] = "grassland",
    [G.GROUND.SAVANNA] = "savanna",
    [G.GROUND.MARSH] = "swamp",
    [G.GROUND.ROCKY] = "rocky",
    [G.GROUND.DECIDUOUS] = "deciduous_forest",
    [G.GROUND.DESERT_DIRT] = "desert",
    [G.GROUND.DIRT] = "dirt",
}

local GRAPH_RESOURCE_PREFABS =
{
    grass = true, sapling = true, berrybush = true, berrybush2 = true,
    evergreen = true, deciduoustree = true, rock1 = true, rock2 = true,
    rock_flintless = true, flint = true, goldnugget = true, carrot = true,
    rabbit = true, beefalo = true, reeds = true, marsh_bush = true,
}

local EXCLUDE_TAGS = { "INLIMBO", "NOCLICK", "FX", "DECOR" }
local RELEVANT_TAGS =
{
    "pickable",
    "CHOP_workable",
    "MINE_workable",
    "DIG_workable",
    "_inventoryitem",
    "hostile",
    "monster",
    "_combat",
    "fire",
    "campfire",
    "cooker",
}

-- Conservative whole-target workloads from the shipped DST scripts. Trees
-- use the largest normal growth stage because growth stage/work-left is not
-- replicated to clients. Known rock prefabs can use their exact maximum.
local DEFAULT_CHOP_WORK = 15
local MINE_WORK_BY_PREFAB =
{
    rock1 = 6,
    rock2 = 6,
    rock_flintless = 6,
    rock_flintless_med = 4,
    rock_flintless_low = 2,
    rock_moon = 6,
    rock_moon_shell = 6,
    moonglass_rock = 6,
    rock_petrified_tree = 3,
    rock_petrified_tree_med = 3,
    rock_petrified_tree_tall = 4,
    rock_petrified_tree_short = 2,
    rock_petrified_tree_old = 1,
}

local TOOL_MAX_USES = { axe = 100, pickaxe = 33 }

local WEAPON_PREFAB_SCORE =
{
    axe = 27,
    pickaxe = 27,
    spear = 34,
    spear_wathgrithr = 42,
    batbat = 42,
    tentaclespike = 51,
    hambat = 59,
    ruins_bat = 59,
    nightsword = 68,
    glasscutter = 68,
}

local RELEVANT_PREFABS =
{
    grass = true,
    sapling = true,
    sapling_moon = true,
    twigs = true,
    cutgrass = true,
    flint = true,
    rocks = true,
    log = true,
    carrot_planted = true,
    carrot = true,
    berries = true,
    berrybush = true,
    berrybush2 = true,
    juicyberrybush = true,
    evergreen = true,
    evergreen_sparse = true,
    deciduoustree = true,
    campfire = true,
    firepit = true,
    torch = true,
    spiderden = true,
    hound = true,
    tentacle = true,
    goldnugget = true,
    nitre = true,
    pinecone = true,
    acorn = true,
    charcoal = true,
    marble = true,
    moonrocknugget = true,
    sciencemachine = true,
}

local PICKABLE_SOURCE_PREFABS =
{
    grass = true,
    sapling = true,
    sapling_moon = true,
    berrybush = true,
    berrybush2 = true,
    juicyberrybush = true,
}

local function round(value, digits)
    if value == nil then
        return nil
    end
    local scale = 10 ^ (digits or 0)
    return math.floor(value * scale + 0.5) / scale
end

local function safe_call(fn, fallback)
    local ok, result = protected_call(fn)
    if ok then
        return result
    end
    return fallback
end

local function read_vitals(player)
    local vitals = {}
    local replica = player.replica

    if replica ~= nil and replica.health ~= nil then
        vitals.health = round(safe_call(function() return replica.health:GetCurrent() end, 0), 1)
        vitals.health_max = round(safe_call(function() return replica.health:Max() end, 0), 1)
        vitals.dead = safe_call(function() return replica.health:IsDead() end, false)
    end
    if replica ~= nil and replica.hunger ~= nil then
        vitals.hunger = round(safe_call(function() return replica.hunger:GetCurrent() end, 0), 1)
        vitals.hunger_max = round(safe_call(function() return replica.hunger:Max() end, 0), 1)
    end
    if replica ~= nil and replica.sanity ~= nil then
        vitals.sanity = round(safe_call(function() return replica.sanity:GetCurrent() end, 0), 1)
        vitals.sanity_max = round(safe_call(function() return replica.sanity:Max() end, 0), 1)
    end

    return vitals
end

local function item_record(item, slot)
    if item == nil or not item:IsValid() then
        return nil
    end

    local count = 1
    if item.replica ~= nil and item.replica.stackable ~= nil then
        count = safe_call(function() return item.replica.stackable:StackSize() end, 1)
    end

    local durability_percent = nil
    if item.components ~= nil and item.components.finiteuses ~= nil then
        durability_percent = safe_call(function() return item.components.finiteuses:GetPercent() end, nil)
    elseif item.replica ~= nil and item.replica.inventoryitem ~= nil then
        local classified = item.replica.inventoryitem.classified
        local encoded = classified ~= nil and classified.percentused ~= nil
            and safe_call(function() return classified.percentused:value() end, 255) or 255
        if encoded ~= 255 then
            durability_percent = encoded / 100
        end
    end

    local weapon_damage = nil
    local weapon = item.prefab ~= "torch"
        and (item:HasTag("weapon") or WEAPON_PREFAB_SCORE[item.prefab or ""] ~= nil)
    if weapon and item.components ~= nil and item.components.weapon ~= nil then
        weapon_damage = safe_call(function() return item.components.weapon:GetDamage(G.ThePlayer, nil) end, nil)
    end
    if weapon_damage == nil then
        weapon_damage = WEAPON_PREFAB_SCORE[item.prefab or ""]
    end

    return
    {
        slot = slot,
        guid = item.GUID,
        prefab = item.prefab or "unknown",
        count = count,
        durability_percent = durability_percent ~= nil and round(durability_percent, 3) or nil,
        max_uses = TOOL_MAX_USES[item.prefab or ""],
        weapon = weapon,
        weapon_damage = weapon_damage ~= nil and round(weapon_damage, 1) or nil,
    }
end

local function read_inventory(player)
    local result = { items = {}, equipped = {} }
    local inventory = player.replica ~= nil and player.replica.inventory or nil
    if inventory == nil then
        return result
    end

    local items = safe_call(function() return inventory:GetItems() end, {}) or {}
    for slot, item in pairs(items) do
        local record = item_record(item, slot)
        if record ~= nil then
            table.insert(result.items, record)
        end
    end
    table.sort(result.items, function(a, b) return a.slot < b.slot end)

    local equips = safe_call(function() return inventory:GetEquips() end, {}) or {}
    for equip_slot, item in pairs(equips) do
        local record = item_record(item, tostring(equip_slot))
        if record ~= nil then
            table.insert(result.equipped, record)
        end
    end
    table.sort(result.equipped, function(a, b) return tostring(a.slot) < tostring(b.slot) end)

    return result
end

local function entity_tags(entity)
    local tags = {}
    for _, tag in ipairs(RELEVANT_TAGS) do
        if entity:HasTag(tag) then
            table.insert(tags, tag)
        end
    end
    return tags
end

local function can_be_picked(entity)
    local pickable = entity.components ~= nil and entity.components.pickable or nil
    if pickable ~= nil then
        return entity:HasTag("pickable")
            and safe_call(function() return pickable:CanBePicked() end, false)
    end
    -- Remote clients do not have the server component, but its current
    -- availability is replicated through the pickable tag.
    return entity:HasTag("pickable")
end

local function is_relevant(entity)
    if RELEVANT_PREFABS[entity.prefab or ""] then
        return true
    end
    for _, tag in ipairs(RELEVANT_TAGS) do
        if entity:HasTag(tag) then
            return true
        end
    end
    return false
end

local function has_nearby_lit_fire(player, radius)
    if player == nil or not player:IsValid() then
        return false
    end
    local x, y, z = player.Transform:GetWorldPosition()
    local entities = G.TheSim:FindEntities(x, y, z, radius or 8, nil, EXCLUDE_TAGS)
    for _, entity in ipairs(entities) do
        if entity:IsValid()
            and (entity.prefab == "campfire" or entity.prefab == "firepit")
            and entity:HasTag("fire") then
            return true
        end
    end
    return false
end

local function read_nearby(player)
    local nearby = {}
    local px, py, pz = player.Transform:GetWorldPosition()
    local entities = G.TheSim:FindEntities(px, py, pz, SCAN_RADIUS, nil, EXCLUDE_TAGS)

    for _, entity in ipairs(entities) do
        if entity ~= player and entity:IsValid() and is_relevant(entity) then
            local x, y, z = entity.Transform:GetWorldPosition()
            local dx = x - px
            local dz = z - pz
            local record =
            {
                guid = entity.GUID,
                prefab = entity.prefab or "unknown",
                distance = round(math.sqrt(dx * dx + dz * dz), 2),
                dx = round(dx, 2),
                dz = round(dz, 2),
                tags = entity_tags(entity),
            }
            if PICKABLE_SOURCE_PREFABS[entity.prefab or ""] then
                record.pickable = can_be_picked(entity)
            end
            if entity:HasTag("CHOP_workable") then
                record.work_required = DEFAULT_CHOP_WORK
            elseif entity:HasTag("MINE_workable") then
                record.work_required = MINE_WORK_BY_PREFAB[entity.prefab or ""]
            end
            local combat = player.replica ~= nil and player.replica.combat or nil
            record.activeThreat = entity.replica ~= nil
                and entity.replica.combat ~= nil
                and safe_call(function()
                    return entity.replica.combat:GetTarget() == player
                end, false)

            record.attackable = combat ~= nil
                and safe_call(function() return combat:CanTarget(entity) end, false)
            record.potentialThreat = record.activeThreat == true
                or (entity:HasTag("hostile") and not entity:HasTag("dead"))
            table.insert(nearby, record)
        end
    end

    table.sort(nearby, function(a, b) return a.distance < b.distance end)
    while #nearby > MAX_NEARBY do
        table.remove(nearby)
    end
    return nearby
end

local function read_crafting(player)
    local result = { torch = false, campfire = false, axe = false, pickaxe = false }
    local builder = player.replica ~= nil and player.replica.builder or nil
    if builder ~= nil then
        result.torch = safe_call(function() return builder:CanBuild("torch") end, false)
        result.campfire = safe_call(function() return builder:CanBuild("campfire") end, false)
        result.axe = safe_call(function() return builder:CanBuild("axe") end, false)
        result.pickaxe = safe_call(function() return builder:CanBuild("pickaxe") end, false)
        result.sciencemachine = safe_call(function() return builder:CanBuild("sciencemachine") end, false)
    end
    return result
end

local function read_camera()
    local camera = G.TheCamera
    if camera == nil then
        return {}
    end

    local right = safe_call(function() return camera:GetRightVec() end, nil)
    local down = safe_call(function() return camera:GetDownVec() end, nil)
    return
    {
        heading = round(safe_call(function() return camera:GetHeading() end, 0), 2),
        right = right ~= nil and { x = round(right.x, 5), z = round(right.z, 5) } or nil,
        forward = down ~= nil and { x = round(-down.x, 5), z = round(-down.z, 5) } or nil,
    }
end

local NAV_NEIGHBORS =
{
    { -1, -1 }, { 0, -1 }, { 1, -1 },
    { -1,  0 },             { 1,  0 },
    { -1,  1 }, { 0,  1 }, { 1,  1 },
}

local function nav_key(gx, gz)
    return tostring(gx) .. ":" .. tostring(gz)
end

local function nav_grid_coordinate(value)
    return math.floor(value / NAV_CELL_SIZE + 0.5)
end

local function get_navigation_memory(world)
    if world._jev_dst_navigation == nil then
        world._jev_dst_navigation =
        {
            cells = {},
            target = nil,
            road_target = nil,
            road_cells = {},
            road_edges = {},
            road_blacklist = {},
            last_road_cell = nil,
            regions = {},
            current_region = nil,
            current_region_name = nil,
            current_biome = nil,
            pending_biome = nil,
            pending_biome_samples = 0,
            biomes = {},
            pending_region = nil,
            pending_region_samples = 0,
            blacklist = {},
            last_player_cell = nil,
            graph_nodes = {},
            graph_edges = {},
            current_node = nil,
            last_node_position = nil,
            return_stack = {},
            hazards = {},
        }
    end
    return world._jev_dst_navigation
end

local function observe_navigation_cells(player, world, memory)
    local map = world.Map
    local px, py, pz = player.Transform:GetWorldPosition()
    local pgx = nav_grid_coordinate(px)
    local pgz = nav_grid_coordinate(pz)
    local radius_cells = math.ceil(NAV_OBSERVE_RADIUS / NAV_CELL_SIZE)
    local now = G.GetTime()

    for gx = pgx - radius_cells, pgx + radius_cells do
        for gz = pgz - radius_cells, pgz + radius_cells do
            local x = gx * NAV_CELL_SIZE
            local z = gz * NAV_CELL_SIZE
            local distance = math.sqrt((x - px) * (x - px) + (z - pz) * (z - pz))
            if distance <= NAV_OBSERVE_RADIUS + NAV_CELL_SIZE * 0.75 then
                local key = nav_key(gx, gz)
                local cell = memory.cells[key] or { gx = gx, gz = gz, x = x, z = z, visits = 0 }
                cell.passable = safe_call(function()
                    return map:IsPassableAtPoint(x, 0, z, false, false)
                end, false)
                cell.region = safe_call(function()
                    local id = map:GetTopologyIDAtPoint(x, 0, z)
                    return id ~= nil and tostring(id) or nil
                end, nil)
                cell.biome = BIOME_BY_TILE[safe_call(function()
                    return map:GetTileAtPoint(x, 0, z)
                end, nil)]
                cell.last_seen = now
                memory.cells[key] = cell
            end
        end
    end

    local player_key = nav_key(pgx, pgz)
    if memory.last_player_cell ~= player_key then
        local cell = memory.cells[player_key]
        if cell ~= nil then
            cell.visits = (cell.visits or 0) + 1
        end
        memory.last_player_cell = player_key
    end
end

local function observe_hazards(player, memory, nearby)
    local px, py, pz = player.Transform:GetWorldPosition()
    local now = G.GetTime()
    for guid, hazard in pairs(memory.hazards) do
        if now - hazard.last_seen > HAZARD_MEMORY_SECONDS then
            memory.hazards[guid] = nil
        end
    end
    for _, entity in ipairs(nearby or {}) do
        if entity.potentialThreat == true and entity.guid ~= nil
            and entity.dx ~= nil and entity.dz ~= nil then
            memory.hazards[entity.guid] = {
                guid = entity.guid, prefab = entity.prefab,
                x = px + entity.dx, z = pz + entity.dz, last_seen = now,
            }
        end
    end
end

local function hazard_penalty(memory, x, z)
    local penalty = 0
    for _, hazard in pairs(memory.hazards) do
        local distance = math.sqrt((x - hazard.x) ^ 2 + (z - hazard.z) ^ 2)
        if distance < HAZARD_CLEARANCE then
            return math.huge
        end
        if distance < HAZARD_PENALTY_RANGE then
            penalty = penalty + (HAZARD_PENALTY_RANGE - distance) * 8
        end
    end
    return penalty
end

local function hazard_segment_unsafe(memory, ax, az, bx, bz)
    local dx, dz = bx - ax, bz - az
    local length_sq = dx * dx + dz * dz
    for _, hazard in pairs(memory.hazards) do
        local start_distance = math.sqrt((ax - hazard.x) ^ 2 + (az - hazard.z) ^ 2)
        local end_distance = math.sqrt((bx - hazard.x) ^ 2 + (bz - hazard.z) ^ 2)
        local projection = length_sq == 0 and 0 or math.max(0, math.min(1,
            ((hazard.x - ax) * dx + (hazard.z - az) * dz) / length_sq))
        local path_x, path_z = ax + projection * dx, az + projection * dz
        local path_distance = math.sqrt((path_x - hazard.x) ^ 2 + (path_z - hazard.z) ^ 2)
        local moving_away = start_distance < HAZARD_CLEARANCE
            and end_distance > start_distance + 0.5
            and path_distance >= start_distance - 0.5
        if not moving_away and (path_distance < HAZARD_CLEARANCE
            or (end_distance < 20 and end_distance < start_distance - 0.5)) then
            return true
        end
    end
    return false
end

local function frontier_candidates(player, memory)
    local px, py, pz = player.Transform:GetWorldPosition()
    local now = G.GetTime()
    local candidates = {}
    for key, cell in pairs(memory.cells) do
        if cell.passable and (memory.current_biome == nil or cell.biome == memory.current_biome)
            and (memory.blacklist[key] or 0) <= now then
            local information_gain = 0
            for _, offset in ipairs(NAV_NEIGHBORS) do
                if memory.cells[nav_key(cell.gx + offset[1], cell.gz + offset[2])] == nil then
                    information_gain = information_gain + 1
                end
            end
            if information_gain > 0 then
                local distance = math.sqrt((cell.x - px) * (cell.x - px) + (cell.z - pz) * (cell.z - pz))
                local danger = hazard_penalty(memory, cell.x, cell.z)
                if distance > NAV_ARRIVAL_DISTANCE and danger < math.huge
                    and not hazard_segment_unsafe(memory, px, pz, cell.x, cell.z) then
                    local score = information_gain * 10 - distance * 0.5
                        - (cell.visits or 0) * 6 - danger
                    table.insert(candidates,
                    {
                        key = key,
                        gx = cell.gx,
                        gz = cell.gz,
                        x = cell.x,
                        z = cell.z,
                        information_gain = information_gain,
                        visits = cell.visits or 0,
                        score = score,
                        distance = distance,
                    })
                end
            end
        end
    end
    table.sort(candidates, function(a, b)
        return a.score == b.score and a.distance < b.distance or a.score > b.score
    end)
    return candidates
end

local function make_navigation_leg(player, map, target, arrival_distance)
    local px, py, pz = player.Transform:GetWorldPosition()
    local dx = target.x - px
    local dz = target.z - pz
    local remaining = math.sqrt(dx * dx + dz * dz)
    if remaining <= (arrival_distance or NAV_ARRIVAL_DISTANCE) then
        return nil, "arrived"
    end
    local leg_distance = math.min(NAV_LEG_DISTANCE, remaining)
    local nx = dx / remaining
    local nz = dz / remaining
    local travelled = NAV_PROBE_STEP
    while travelled < leg_distance do
        if not safe_call(function()
            return map:IsPassableAtPoint(px + nx * travelled, 0, pz + nz * travelled, false, false)
        end, false) then
            return nil, "blocked"
        end
        travelled = travelled + NAV_PROBE_STEP
    end
    local leg_x = px + nx * leg_distance
    local leg_z = pz + nz * leg_distance
    if not safe_call(function()
        return map:IsPassableAtPoint(leg_x, 0, leg_z, false, false)
    end, false) then
        return nil, "blocked"
    end
    return
    {
        x = leg_x,
        z = leg_z,
        dx = leg_x - px,
        dz = leg_z - pz,
        distance = leg_distance,
    }, nil
end

local function read_escape_routes(player, map, nearby)
    local threatened = false
    for _, entity in ipairs(nearby or {}) do
        if entity.activeThreat == true and (entity.distance or math.huge) <= 12 then
            threatened = true
            break
        end
    end
    local routes = {}
    if not threatened then
        return routes
    end
    local camera = read_camera()
    if camera.right == nil or camera.forward == nil then
        return routes
    end
    local px, py, pz = player.Transform:GetWorldPosition()
    for _, direction in ipairs(NAV_NEIGHBORS) do
        local dx = direction[1] * camera.right.x + direction[2] * camera.forward.x
        local dz = direction[1] * camera.right.z + direction[2] * camera.forward.z
        local length = math.sqrt(dx * dx + dz * dz)
        dx, dz = dx / length, dz / length
        local safe_distance = 0
        for distance = NAV_PROBE_STEP, ESCAPE_PROBE_DISTANCE, NAV_PROBE_STEP do
            local passable = true
            for _, side in ipairs({ -0.3, 0, 0.3 }) do
                local x = px + dx * distance - dz * side
                local z = pz + dz * distance + dx * side
                if not safe_call(function()
                    return map:IsPassableAtPoint(x, 0, z, false, false)
                end, false) then
                    passable = false
                    break
                end
            end
            if not passable then
                break
            end
            safe_distance = distance
        end
        if safe_distance >= 1.5 then
            table.insert(routes, { dx = round(dx, 3), dz = round(dz, 3),
                distance = round(safe_distance, 2) })
        end
    end
    return routes
end

local observe_current_region
local observe_current_biome
local observe_road_cells
local select_road_target
local observe_area_graph
local read_area_graph

local function read_navigation(player, nearby)
    local world = G.TheWorld
    local map = world ~= nil and world.Map or nil
    if world == nil or map == nil then
        return {}
    end
    local memory = get_navigation_memory(world)
    local px, py, pz = player.Transform:GetWorldPosition()
    local current_ground_tile = safe_call(function()
        return map:GetTileAtPoint(px, 0, pz)
    end, nil)
    observe_current_region(player, map, memory)
    observe_current_biome(player, map, memory)
    observe_navigation_cells(player, world, memory)
    observe_hazards(player, memory, nearby)
    observe_area_graph(player, map, memory, nearby)
    local on_road = observe_road_cells(player, map, memory)
    local candidates = frontier_candidates(player, memory)
    local now = G.GetTime()
    local leg = nil
    local status = "no_frontier"

    if memory.target ~= nil then
        leg, status = make_navigation_leg(player, map, memory.target)
        if hazard_penalty(memory, memory.target.x, memory.target.z) == math.huge
            or hazard_segment_unsafe(memory, px, pz, memory.target.x, memory.target.z)
            or (leg ~= nil and hazard_segment_unsafe(memory, px, pz, leg.x, leg.z)) then
            memory.target = nil
            leg = nil
            status = "hazard"
        elseif status == "arrived" then
            memory.target = nil
        elseif status == "blocked" then
            memory.blacklist[memory.target.key] = now + NAV_BLACKLIST_SECONDS
            memory.target = nil
        end
    end

    if memory.target == nil then
        for _, candidate in ipairs(candidates) do
            if (memory.blacklist[candidate.key] or 0) <= now then
                local candidate_leg, candidate_status = make_navigation_leg(player, map, candidate)
                if candidate_leg ~= nil
                    and not hazard_segment_unsafe(memory, px, pz,
                        candidate_leg.x, candidate_leg.z) then
                    memory.target = candidate
                    leg = candidate_leg
                    status = "ready"
                    break
                elseif candidate_status == "blocked" then
                    memory.blacklist[candidate.key] = now + NAV_BLACKLIST_SECONDS
                end
            end
        end
    elseif leg ~= nil then
        status = "ready"
    end

    local observed_cells = 0
    for _ in pairs(memory.cells) do
        observed_cells = observed_cells + 1
    end
    local target = memory.target
    if target ~= nil then
        local px, py, pz = player.Transform:GetWorldPosition()
        target.distance = math.sqrt((target.x - px) * (target.x - px) + (target.z - pz) * (target.z - pz))
    end
    local road_target, road_leg, road_status = select_road_target(player, map, memory, on_road)
    local area_graph = read_area_graph(player, map, memory)
    if road_leg ~= nil and hazard_segment_unsafe(memory, px, pz,
        road_leg.x, road_leg.z) then
        road_target, road_leg, road_status = nil, nil, "hazard"
    end
    local known_regions = 0
    for _ in pairs(memory.regions) do
        known_regions = known_regions + 1
    end
    local known_biomes = 0
    local biomes_seen = {}
    for biome in pairs(memory.biomes) do
        known_biomes = known_biomes + 1
        table.insert(biomes_seen, biome)
    end
    table.sort(biomes_seen)
    local hazards = {}
    for _, hazard in pairs(memory.hazards) do
        table.insert(hazards, { guid = hazard.guid, prefab = hazard.prefab,
            x = round(hazard.x, 2), z = round(hazard.z, 2),
            age = round(G.GetTime() - hazard.last_seen, 1) })
    end
    table.sort(hazards, function(a, b)
        return (a.x - px) ^ 2 + (a.z - pz) ^ 2
            < (b.x - px) ^ 2 + (b.z - pz) ^ 2
    end)
    while #hazards > 12 do
        table.remove(hazards)
    end
    return
    {
        mode = "region_and_road",
        current_region = memory.current_region,
        current_region_name = memory.current_region_name,
        known_regions = known_regions,
        current_biome = memory.current_biome,
        current_ground_tile = current_ground_tile,
        known_biomes = known_biomes,
        biomes_seen = biomes_seen,
        graph = area_graph,
        hazards = hazards,
        escape_routes = read_escape_routes(player, map, nearby),
        on_road = on_road,
        status = status,
        cell_size = NAV_CELL_SIZE,
        observed_cells = observed_cells,
        frontier_count = #candidates,
        target = target ~= nil and
        {
            x = round(target.x, 2),
            z = round(target.z, 2),
            distance = round(target.distance, 2),
            information_gain = target.information_gain,
            visits = target.visits,
            score = round(target.score, 2),
        } or nil,
        leg = leg ~= nil and
        {
            x = round(leg.x, 2),
            z = round(leg.z, 2),
            dx = round(leg.dx, 2),
            dz = round(leg.dz, 2),
            distance = round(leg.distance, 2),
        } or nil,
        road =
        {
            status = road_status,
            target = road_target ~= nil and
            {
                x = round(road_target.x, 2),
                z = round(road_target.z, 2),
                distance = round(road_target.distance, 2),
            } or nil,
            leg = road_leg ~= nil and
            {
                x = round(road_leg.x, 2),
                z = round(road_leg.z, 2),
                dx = round(road_leg.dx, 2),
                dz = round(road_leg.dz, 2),
                distance = round(road_leg.distance, 2),
            } or nil,
        },
    }
end

local function observed_region_at(map, x, z)
    local id = safe_call(function()
        local topology_id = map:GetTopologyIDAtPoint(x, 0, z)
        return topology_id
    end, nil)
    if id ~= nil and id ~= "" then
        return tostring(id)
    end
    return nil
end

observe_current_region = function(player, map, memory)
    local px, py, pz = player.Transform:GetWorldPosition()
    local observed = observed_region_at(map, px, pz)
    if observed == nil then
        return
    end
    if memory.current_region == nil then
        memory.current_region = observed
        local data = safe_call(function() return G.ConvertTopologyIdToData(observed) end, {}) or {}
        memory.current_region_name = data.task_id or data.layout_id or observed
    elseif observed ~= memory.current_region then
        if memory.pending_region == observed then
            memory.pending_region_samples = memory.pending_region_samples + 1
        else
            memory.pending_region = observed
            memory.pending_region_samples = 1
        end
        if memory.pending_region_samples >= 2 then
            memory.current_region = observed
            local data = safe_call(function() return G.ConvertTopologyIdToData(observed) end, {}) or {}
            memory.current_region_name = data.task_id or data.layout_id or observed
            memory.pending_region = nil
            memory.pending_region_samples = 0
            memory.road_target = nil
        end
    else
        memory.pending_region = nil
        memory.pending_region_samples = 0
    end
    local region = memory.regions[memory.current_region] or { observations = 0 }
    region.observations = region.observations + 1
    memory.regions[memory.current_region] = region
end

local function biome_at(map, x, z)
    local tile = safe_call(function() return map:GetTileAtPoint(x, 0, z) end, nil)
    return BIOME_BY_TILE[tile]
end

observe_current_biome = function(player, map, memory)
    local px, py, pz = player.Transform:GetWorldPosition()
    local observed = biome_at(map, px, pz)
    if observed == nil then
        -- A road or artificial floor hides the natural tile. Sample nearby
        -- ground rather than treating the road as its own biome.
        local votes = {}
        for _, offset in ipairs(NAV_NEIGHBORS) do
            local neighbor = biome_at(map, px + offset[1] * 3, pz + offset[2] * 3)
            if neighbor ~= nil then
                votes[neighbor] = (votes[neighbor] or 0) + 1
            end
        end
        local best_count = 0
        for biome, count in pairs(votes) do
            if count > best_count or (count == best_count and biome == memory.current_biome) then
                observed = biome
                best_count = count
            end
        end
    end
    if observed == nil then
        return
    end
    if memory.current_biome == nil then
        memory.current_biome = observed
    elseif observed ~= memory.current_biome then
        if memory.pending_biome == observed then
            memory.pending_biome_samples = memory.pending_biome_samples + 1
        else
            memory.pending_biome = observed
            memory.pending_biome_samples = 1
        end
        if memory.pending_biome_samples >= 2 then
            memory.current_biome = observed
            memory.pending_biome = nil
            memory.pending_biome_samples = 0
            memory.target = nil
        end
    else
        memory.pending_biome = nil
        memory.pending_biome_samples = 0
    end
    memory.biomes[memory.current_biome] = true
end

local function graph_node_id(region, biome)
    return region ~= nil and biome ~= nil and region .. "|" .. biome or nil
end

local function ensure_graph_node(memory, id, region, biome)
    local node = memory.graph_nodes[id]
    if node == nil then
        node = { id = id, region = region, biome = biome, observed_cells = 0,
            entries = 0, resources = {} }
        memory.graph_nodes[id] = node
    end
    return node
end

local function graph_segment_passable(map, ax, az, bx, bz)
    local dx, dz = bx - ax, bz - az
    local distance = math.sqrt(dx * dx + dz * dz)
    local steps = math.max(1, math.ceil(distance / NAV_PROBE_STEP))
    for step = 0, steps do
        local fraction = step / steps
        if not safe_call(function()
            return map:IsPassableAtPoint(ax + dx * fraction, 0, az + dz * fraction,
                false, false)
        end, false) then
            return false
        end
    end
    return true
end

local function graph_segment_in_node(map, ax, az, bx, bz, node_id)
    local dx, dz = bx - ax, bz - az
    local distance = math.sqrt(dx * dx + dz * dz)
    local steps = math.max(1, math.ceil(distance / NAV_PROBE_STEP))
    for step = 0, steps do
        local fraction = step / steps
        local x, z = ax + dx * fraction, az + dz * fraction
        local observed = graph_node_id(observed_region_at(map, x, z), biome_at(map, x, z))
        if observed ~= nil and observed ~= node_id then
            return false
        end
    end
    return true
end

local function graph_edge_key(a_id, b_id)
    return a_id < b_id and a_id .. ">" .. b_id or b_id .. ">" .. a_id
end

local function add_graph_edge(memory, map, a_id, ax, az, b_id, bx, bz)
    if a_id == nil or b_id == nil or a_id == b_id then
        return false
    end
    if b_id < a_id then
        a_id, b_id = b_id, a_id
        ax, bx = bx, ax
        az, bz = bz, az
    end
    local key = graph_edge_key(a_id, b_id)
    local entrances = memory.graph_edges[key] or {}
    for _, entrance in ipairs(entrances) do
        if math.sqrt((entrance.ax - ax) ^ 2 + (entrance.az - az) ^ 2) < NAV_CELL_SIZE then
            return true
        end
    end
    if #entrances >= 3 or not graph_segment_passable(map, ax, az, bx, bz) then
        return false
    end
    if #entrances < 3 then
        table.insert(entrances, { a_id = a_id, b_id = b_id,
            ax = ax, az = az, bx = bx, bz = bz })
        memory.graph_edges[key] = entrances
    end
    return true
end

observe_area_graph = function(player, map, memory, nearby)
    local px, py, pz = player.Transform:GetWorldPosition()
    local pgx = nav_grid_coordinate(px)
    local pgz = nav_grid_coordinate(pz)
    local radius = math.ceil(NAV_OBSERVE_RADIUS / NAV_CELL_SIZE)
    for gx = pgx - radius, pgx + radius do
        for gz = pgz - radius, pgz + radius do
            local cell = memory.cells[nav_key(gx, gz)]
            if cell ~= nil and cell.passable
                and math.sqrt((cell.x - px) ^ 2 + (cell.z - pz) ^ 2)
                    <= NAV_OBSERVE_RADIUS + NAV_CELL_SIZE * 0.75 then
                cell.node = graph_node_id(cell.region, cell.biome)
                if cell.node ~= nil then
                    local node = ensure_graph_node(memory, cell.node, cell.region, cell.biome)
                    if cell.graph_counted_node ~= cell.node then
                        node.observed_cells = node.observed_cells + 1
                        cell.graph_counted_node = cell.node
                    end
                    for _, offset in ipairs(NAV_NEIGHBORS) do
                        local neighbor = memory.cells[nav_key(gx + offset[1], gz + offset[2])]
                        if neighbor ~= nil and neighbor.passable then
                            local neighbor_id = graph_node_id(neighbor.region, neighbor.biome)
                            if neighbor_id ~= nil and neighbor_id ~= cell.node then
                                ensure_graph_node(memory, neighbor_id, neighbor.region, neighbor.biome)
                                add_graph_edge(memory, map, cell.node, cell.x, cell.z,
                                    neighbor_id, neighbor.x, neighbor.z)
                            end
                        end
                    end
                end
            end
        end
    end

    local current_id = graph_node_id(memory.current_region, memory.current_biome)
    local raw_region = observed_region_at(map, px, pz)
    local raw_biome = biome_at(map, px, pz)
    if current_id == nil or (raw_region ~= nil and raw_region ~= memory.current_region)
        or (raw_biome ~= nil and raw_biome ~= memory.current_biome) then
        return
    end
    local node = ensure_graph_node(memory, current_id,
        memory.current_region, memory.current_biome)
    if memory.current_node ~= current_id then
        local old_id = memory.current_node
        if old_id ~= nil then
            local old_position = memory.last_node_position
            local linked = memory.graph_edges[graph_edge_key(old_id, current_id)] ~= nil
            if not linked and old_position ~= nil
                and math.sqrt((old_position.x - px) ^ 2 + (old_position.z - pz) ^ 2) <= 8 then
                linked = add_graph_edge(memory, map, old_id, old_position.x,
                    old_position.z, current_id, px, pz)
            end
            if linked then
                local stack = memory.return_stack
                if stack[#stack] == current_id then
                    table.remove(stack)
                else
                    table.insert(stack, old_id)
                end
            end
        end
        memory.current_node = current_id
        node.entries = node.entries + 1
    end
    memory.last_node_position = { x = px, z = pz }

    for _, entity in ipairs(nearby or {}) do
        local prefab = entity.prefab
        if GRAPH_RESOURCE_PREFABS[prefab] and entity.dx ~= nil and entity.dz ~= nil then
            local x, z = px + entity.dx, pz + entity.dz
            local region = observed_region_at(map, x, z)
            local biome = biome_at(map, x, z)
            local id = graph_node_id(region, biome)
            if id ~= nil then
                ensure_graph_node(memory, id, region, biome).resources[prefab] = true
            end
        end
    end
end

local function graph_resources(node)
    local resources = {}
    for prefab in pairs(node.resources) do
        table.insert(resources, prefab)
    end
    table.sort(resources)
    return resources
end

local function graph_route_leg(player, map, memory, current_id, side_x, side_z,
    destination_x, destination_z)
    local px, py, pz = player.Transform:GetWorldPosition()
    local distance_to_side = math.sqrt((side_x - px) ^ 2 + (side_z - pz) ^ 2)
    if distance_to_side <= NAV_ARRIVAL_DISTANCE then
        return make_navigation_leg(player, map,
            { x = destination_x, z = destination_z }, NAV_ARRIVAL_DISTANCE)
    end
    local direct = make_navigation_leg(player, map,
        { x = side_x, z = side_z }, NAV_ARRIVAL_DISTANCE)
    if direct ~= nil and graph_segment_in_node(map, px, pz,
        direct.x, direct.z, current_id) then
        return direct
    end

    -- If the direct line is blocked, use only observed cells of this node.
    local start_key, start_distance = nil, math.huge
    local goal_key, goal_distance = nil, math.huge
    for key, cell in pairs(memory.cells) do
        if cell.passable and cell.node == current_id then
            local from_player = math.sqrt((cell.x - px) ^ 2 + (cell.z - pz) ^ 2)
            if from_player < start_distance and from_player <= NAV_CELL_SIZE * 1.5
                and graph_segment_passable(map, px, pz, cell.x, cell.z)
                and graph_segment_in_node(map, px, pz, cell.x, cell.z, current_id) then
                start_key, start_distance = key, from_player
            end
            local from_side = math.sqrt((cell.x - side_x) ^ 2 + (cell.z - side_z) ^ 2)
            if from_side < goal_distance then
                goal_key, goal_distance = key, from_side
            end
        end
    end
    if start_key == nil or goal_key == nil or goal_distance > NAV_CELL_SIZE then
        return nil
    end
    local queue, parent, head = { start_key }, { [start_key] = false }, 1
    while head <= #queue and parent[goal_key] == nil do
        local key = queue[head]
        head = head + 1
        local cell = memory.cells[key]
        for _, offset in ipairs(NAV_NEIGHBORS) do
            local next_key = nav_key(cell.gx + offset[1], cell.gz + offset[2])
            local neighbor = memory.cells[next_key]
            if parent[next_key] == nil and neighbor ~= nil and neighbor.passable
                and neighbor.node == current_id and graph_segment_passable(map,
                    cell.x, cell.z, neighbor.x, neighbor.z) and graph_segment_in_node(
                    map, cell.x, cell.z, neighbor.x, neighbor.z, current_id) then
                parent[next_key] = key
                table.insert(queue, next_key)
            end
        end
    end
    if parent[goal_key] == nil then
        return nil
    end
    local path = {}
    local next_key = goal_key
    while next_key ~= nil and next_key ~= false do
        table.insert(path, 1, next_key)
        next_key = parent[next_key]
    end
    for index = 2, #path do
        local next_cell = memory.cells[path[index]]
        local candidate = make_navigation_leg(player, map, next_cell,
            NAV_ARRIVAL_DISTANCE)
        if candidate ~= nil and graph_segment_in_node(map, px, pz,
            candidate.x, candidate.z, current_id) then
            return candidate
        end
    end
    return nil
end

read_area_graph = function(player, map, memory)
    local current_id = memory.current_node
    local node = current_id ~= nil and memory.graph_nodes[current_id] or nil
    local graph = { current_node = current_id, known_nodes = 0,
        adjacent = {}, return_route = nil }
    for _ in pairs(memory.graph_nodes) do
        graph.known_nodes = graph.known_nodes + 1
    end
    if node == nil then
        return graph
    end
    graph.current = { id = node.id, biome = node.biome, topology = node.region,
        observed_cells = node.observed_cells, entries = node.entries,
        resources_seen = graph_resources(node) }
    local px, py, pz = player.Transform:GetWorldPosition()
    local routes = {}
    for _, entrances in pairs(memory.graph_edges) do
        for _, entrance in ipairs(entrances) do
            if entrance.a_id == current_id or entrance.b_id == current_id then
                local other_id = entrance.a_id == current_id and entrance.b_id or entrance.a_id
                local side_x = entrance.a_id == current_id and entrance.ax or entrance.bx
                local side_z = entrance.a_id == current_id and entrance.az or entrance.bz
                local other_x = entrance.a_id == current_id and entrance.bx or entrance.ax
                local other_z = entrance.a_id == current_id and entrance.bz or entrance.az
                local leg = graph_route_leg(player, map, memory, current_id,
                    side_x, side_z, other_x, other_z)
                if leg ~= nil and not hazard_segment_unsafe(memory, px, pz,
                    leg.x, leg.z) and hazard_penalty(memory, leg.x, leg.z) < math.huge
                    and hazard_penalty(memory, other_x, other_z) < math.huge then
                    local other = memory.graph_nodes[other_id]
                    local distance = math.sqrt((side_x - px) ^ 2 + (side_z - pz) ^ 2)
                    local route = { status = "ready", node_id = other_id,
                        biome = other.biome, topology = other.region,
                        entered = other.entries > 0, resources_seen = graph_resources(other),
                        distance = round(distance, 2),
                        target = { x = round(other_x, 2), z = round(other_z, 2) },
                        leg = { x = round(leg.x, 2), z = round(leg.z, 2),
                            distance = round(leg.distance, 2) } }
                    local previous = routes[other_id]
                    if previous == nil or route.distance < previous.distance then
                        routes[other_id] = route
                    end
                end
            end
        end
    end
    local return_id = memory.return_stack[#memory.return_stack]
    graph.return_route = return_id ~= nil and routes[return_id] or nil
    for id, route in pairs(routes) do
        if id ~= return_id then
            table.insert(graph.adjacent, route)
        end
    end
    table.sort(graph.adjacent, function(a, b)
        return a.distance == b.distance and a.node_id < b.node_id
            or a.distance < b.distance
    end)
    while #graph.adjacent > 4 do
        table.remove(graph.adjacent)
    end
    return graph
end

local function is_road_point(map, x, z)
    local road_manager = G.RoadManager
    if road_manager ~= nil and safe_call(function()
        return road_manager:IsOnRoad(x, 0, z)
    end, false) then
        return true
    end
    local tile = safe_call(function() return map:GetTileAtPoint(x, 0, z) end, nil)
    return tile ~= nil and G.GROUND_ROADWAYS ~= nil and G.GROUND_ROADWAYS[tile] == true
end

observe_road_cells = function(player, map, memory)
    local px, py, pz = player.Transform:GetWorldPosition()
    local pgx = math.floor(px / ROAD_CELL_SIZE + 0.5)
    local pgz = math.floor(pz / ROAD_CELL_SIZE + 0.5)
    local road_here = is_road_point(map, px, pz)
    local player_key = nav_key(pgx, pgz)
    if road_here and memory.last_road_cell ~= player_key then
        local cell = memory.road_cells[player_key] or
            { gx = pgx, gz = pgz, x = px, z = pz, visits = 0 }
        cell.x = px
        cell.z = pz
        cell.gx = pgx
        cell.gz = pgz
        cell.road = true
        cell.visits = (cell.visits or 0) + 1
        memory.road_cells[player_key] = cell
        memory.last_road_cell = player_key
    elseif not road_here then
        memory.last_road_cell = nil
    end
    local scan = {}
    for gx = pgx - 4, pgx + 4 do
        for gz = pgz - 4, pgz + 4 do
            local x = gx * ROAD_CELL_SIZE
            local z = gz * ROAD_CELL_SIZE
            local distance = math.sqrt((x - px) * (x - px) + (z - pz) * (z - pz))
            local key = nav_key(gx, gz)
            if distance <= ROAD_SCAN_RADIUS and memory.road_cells[key] == nil then
                table.insert(scan, { key = key, x = x, z = z, distance = distance })
            end
        end
    end
    table.sort(scan, function(a, b) return a.distance < b.distance end)
    for i = 1, math.min(ROAD_SCAN_BUDGET, #scan) do
        local sample = scan[i]
        memory.road_cells[sample.key] =
        {
            gx = math.floor(sample.x / ROAD_CELL_SIZE + 0.5),
            gz = math.floor(sample.z / ROAD_CELL_SIZE + 0.5),
            x = sample.x,
            z = sample.z,
            road = is_road_point(map, sample.x, sample.z),
            visits = 0,
        }
    end
    return road_here
end

select_road_target = function(player, map, memory, on_road)
    local px, py, pz = player.Transform:GetWorldPosition()
    local now = G.GetTime()
    local target = memory.road_target
    local leg = nil
    local status = "no_road"
    if target ~= nil then
        leg, status = make_navigation_leg(player, map, target, ROAD_ARRIVAL_DISTANCE)
        if status == "arrived" then
            local cell = memory.road_cells[target.key]
            if cell ~= nil and on_road then
                cell.visits = (cell.visits or 0) + 1
            else
                memory.road_blacklist[target.key] = now + NAV_BLACKLIST_SECONDS
            end
            memory.road_target = nil
        elseif status == "blocked" then
            memory.road_blacklist[target.key] = now + NAV_BLACKLIST_SECONDS
            memory.road_target = nil
        end
    end
    if memory.road_target == nil then
        local starts = {}
        for key, cell in pairs(memory.road_cells) do
            if cell.road and (memory.road_blacklist[key] or 0) <= now then
                local distance = math.sqrt((cell.x - px) * (cell.x - px) + (cell.z - pz) * (cell.z - pz))
                if distance <= (on_road and 3 or ROAD_SCAN_RADIUS)
                    and (not on_road or distance < 0.5
                        or is_road_point(map, (px + cell.x) / 2, (pz + cell.z) / 2)) then
                    table.insert(starts, { key = key, distance = distance })
                end
            end
        end
        table.sort(starts, function(a, b) return a.distance < b.distance end)
        for _, start in ipairs(starts) do
            local queue = { start.key }
            local parent = { [start.key] = false }
            local head = 1
            local goal_key = nil
            while head <= #queue do
                local key = queue[head]
                head = head + 1
                local cell = memory.road_cells[key]
                if (cell.visits or 0) == 0 and (memory.road_blacklist[key] or 0) <= now then
                    goal_key = key
                    break
                end
                for _, offset in ipairs(NAV_NEIGHBORS) do
                    local next_key = nav_key(cell.gx + offset[1], cell.gz + offset[2])
                    local next_cell = memory.road_cells[next_key]
                    if parent[next_key] == nil and next_cell ~= nil and next_cell.road
                        and (memory.road_blacklist[next_key] or 0) <= now then
                        local edge_key = key < next_key and key .. ">" .. next_key
                            or next_key .. ">" .. key
                        local connected = memory.road_edges[edge_key]
                        if connected == nil then
                            connected = is_road_point(map, (cell.x + next_cell.x) / 2,
                                (cell.z + next_cell.z) / 2)
                            if connected then
                                memory.road_edges[edge_key] = true
                            end
                        end
                        if connected then
                            parent[next_key] = key
                            table.insert(queue, next_key)
                        end
                    end
                end
            end
            if goal_key ~= nil then
                local next_key = goal_key
                while parent[next_key] ~= false and parent[next_key] ~= start.key do
                    next_key = parent[next_key]
                end
                local cell = memory.road_cells[next_key]
                local candidate =
                {
                    key = next_key,
                    x = cell.x,
                    z = cell.z,
                }
                local candidate_leg, candidate_status = make_navigation_leg(
                    player, map, candidate, ROAD_ARRIVAL_DISTANCE)
                if candidate_leg ~= nil then
                    memory.road_target = candidate
                    leg = candidate_leg
                    status = "ready"
                    break
                elseif candidate_status == "blocked" then
                    memory.road_blacklist[candidate.key] = now + NAV_BLACKLIST_SECONDS
                end
            end
        end
    elseif leg ~= nil then
        status = "ready"
    end
    target = memory.road_target
    if target ~= nil then
        target.distance = math.sqrt((target.x - px) * (target.x - px) + (target.z - pz) * (target.z - pz))
    end
    return target, leg, status
end

local function make_state(player)
    local x, y, z = player.Transform:GetWorldPosition()
    local world = G.TheWorld ~= nil and G.TheWorld.state or {}
    local camera = read_camera()
    local nearby = read_nearby(player)

    return
    {
        schema = 7,
        observed_at = round(G.GetTime(), 3),
        player =
        {
            guid = player.GUID,
            prefab = player.prefab or "unknown",
            position = { x = round(x, 2), y = round(y, 2), z = round(z, 2) },
            vitals = read_vitals(player),
            inventory = read_inventory(player),
        },
        world =
        {
            day = (world.cycles or 0) + 1,
            cycles = world.cycles or 0,
            phase = world.phase or "unknown",
            time = round(world.time or 0, 4),
            time_in_phase = round(world.timeinphase or 0, 4),
            is_day = world.isday == true,
            is_dusk = world.isdusk == true,
            is_night = world.isnight == true,
        },
        camera = camera,
        work = player._jev_dst_work,
        navigation = read_navigation(player, nearby),
        crafting = read_crafting(player),
        nearby = nearby,
    }
end

local function emit_state(player)
    if player == nil or not player:IsValid() or player ~= G.ThePlayer then
        return
    end

    local ok, encoded = protected_call(function() return json.encode(make_state(player)) end)
    if ok then
        if #encoded <= MAX_LOG_PAYLOAD_BYTES then
            print(LOG_PREFIX .. encoded)
        else
            local chunks = {}
            local first = 1
            while first <= #encoded do
                local last = math.min(first + MAX_LOG_PAYLOAD_BYTES - 1, #encoded)
                while last < #encoded do
                    local next_byte = string.byte(encoded, last + 1)
                    if next_byte < 128 or next_byte > 191 then
                        break
                    end
                    last = last - 1
                end
                table.insert(chunks, string.sub(encoded, first, last))
                first = last + 1
            end
            telemetry_sequence = telemetry_sequence + 1
            local frame_id = tostring(player.GUID) .. "-" .. tostring(telemetry_sequence)
            for index, chunk in ipairs(chunks) do
                print(CHUNK_PREFIX .. frame_id .. ":" .. tostring(index) .. ":"
                    .. tostring(#chunks) .. ":" .. chunk)
            end
        end
    else
        print("[JEV_DST_ERROR]state_encode_failed:" .. tostring(encoded))
    end
end

local function reject_current_frontier()
    local player = G.ThePlayer
    local world = G.TheWorld
    if player == nil or not player:IsValid() or world == nil then
        print("[JEV_DST_ACTION_ERROR]reject_frontier:world_or_player_unavailable")
        return
    end
    local memory = get_navigation_memory(world)
    if memory.target == nil then
        print("[JEV_DST_ACTION]reject_frontier:no_target")
        return
    end
    local key = memory.target.key
    memory.blacklist[key] = G.GetTime() + NAV_BLACKLIST_SECONDS
    memory.target = nil
    print("[JEV_DST_ACTION]reject_frontier:blacklisted key=" .. tostring(key))
    emit_state(player)
end

local function reject_current_road_target()
    local player = G.ThePlayer
    local world = G.TheWorld
    if player == nil or not player:IsValid() or world == nil then
        print("[JEV_DST_ACTION_ERROR]reject_road:world_or_player_unavailable")
        return
    end
    local memory = get_navigation_memory(world)
    if memory.road_target == nil then
        print("[JEV_DST_ACTION]reject_road:no_target")
        return
    end
    local key = memory.road_target.key
    memory.road_blacklist[key] = G.GetTime() + NAV_BLACKLIST_SECONDS
    memory.road_target = nil
    print("[JEV_DST_ACTION]reject_road:blacklisted key=" .. tostring(key))
    emit_state(player)
end

local function start_telemetry(player)
    if player._jev_dst_telemetry_task ~= nil then
        return
    end

    print("[JEV_DST]telemetry_started interval=" .. tostring(SAMPLE_INTERVAL)
        .. " radius=" .. tostring(SCAN_RADIUS))
    emit_state(player)
    player._jev_dst_telemetry_task = player:DoPeriodicTask(SAMPLE_INTERVAL, emit_state)

    player:ListenForEvent("onremove", function()
        if player._jev_dst_telemetry_task ~= nil then
            player._jev_dst_telemetry_task:Cancel()
            player._jev_dst_telemetry_task = nil
        end
    end)
end

local function craft_torch()
    local player = G.ThePlayer
    if player == nil or not player:IsValid() then
        print("[JEV_DST_ACTION_ERROR]craft_torch:no_local_player")
        return
    end

    local builder = player.replica ~= nil and player.replica.builder or nil
    local recipe = G.GetValidRecipe("torch")
    if builder == nil or recipe == nil then
        print("[JEV_DST_ACTION_ERROR]craft_torch:builder_or_recipe_unavailable")
        return
    end
    if builder:IsBusy() then
        print("[JEV_DST_ACTION_ERROR]craft_torch:builder_busy")
        return
    end
    if not builder:CanBuild("torch") then
        print("[JEV_DST_ACTION_ERROR]craft_torch:not_craftable")
        return
    end

    builder:MakeRecipeFromMenu(recipe)
    print("[JEV_DST_ACTION]craft_torch:requested")
end

local function craft_item(recipe_name)
    local player = G.ThePlayer
    if player == nil or not player:IsValid() then
        print("[JEV_DST_ACTION_ERROR]craft_" .. recipe_name .. ":no_local_player")
        return
    end

    local builder = player.replica ~= nil and player.replica.builder or nil
    local recipe = G.GetValidRecipe(recipe_name)
    if builder == nil or recipe == nil then
        print("[JEV_DST_ACTION_ERROR]craft_" .. recipe_name .. ":builder_or_recipe_unavailable")
        return
    end
    if builder:IsBusy() or not builder:CanBuild(recipe_name) then
        print("[JEV_DST_ACTION_ERROR]craft_" .. recipe_name .. ":not_craftable_or_busy")
        return
    end

    builder:MakeRecipeFromMenu(recipe)
    print("[JEV_DST_ACTION]craft_" .. recipe_name .. ":requested")
end

local function find_nearest_with_tag(player, radius, tag)
    local x, y, z = player.Transform:GetWorldPosition()
    local entities = G.TheSim:FindEntities(x, y, z, radius, { tag }, EXCLUDE_TAGS)
    local nearest = nil
    local nearest_distance = nil
    for _, entity in ipairs(entities) do
        if entity ~= player and entity:IsValid() then
            local distance = player:GetDistanceSqToInst(entity)
            if nearest == nil or distance < nearest_distance then
                nearest = entity
                nearest_distance = distance
            end
        end
    end
    return nearest
end

local function durability_percent(item)
    if item == nil or not item:IsValid() then
        return nil
    end
    if item.components ~= nil and item.components.finiteuses ~= nil then
        return safe_call(function() return item.components.finiteuses:GetPercent() end, nil)
    end
    local classified = item.replica ~= nil and item.replica.inventoryitem ~= nil
        and item.replica.inventoryitem.classified or nil
    local encoded = classified ~= nil and classified.percentused ~= nil
        and safe_call(function() return classified.percentused:value() end, 255) or 255
    return encoded ~= 255 and encoded / 100 or nil
end

local function conservative_uses(item, tool_prefab)
    local percent = durability_percent(item)
    local maximum = TOOL_MAX_USES[tool_prefab]
    if percent == nil or maximum == nil then
        return 0
    end
    local encoded = math.floor(percent * 100 + 0.5)
    for uses = 0, maximum do
        if math.floor(uses / maximum * 100 + 0.5) == encoded then
            return uses
        end
    end
    return 0
end

local function required_work(target, target_tag)
    if target_tag == "CHOP_workable" then
        return DEFAULT_CHOP_WORK
    end
    return MINE_WORK_BY_PREFAB[target.prefab or ""]
end

local function find_sufficient_tool(inventory, tool_prefab, required)
    local best = nil
    local best_uses = -1
    local equipped = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
    if equipped ~= nil and equipped.prefab == tool_prefab then
        best = equipped
        best_uses = conservative_uses(equipped, tool_prefab)
    end
    local items = safe_call(function() return inventory:GetItems() end, {}) or {}
    for _, inventory_tool in pairs(items) do
        if inventory_tool ~= nil and inventory_tool:IsValid() and inventory_tool.prefab == tool_prefab then
            local uses = conservative_uses(inventory_tool, tool_prefab)
            if uses > best_uses then
                best = inventory_tool
                best_uses = uses
            end
        end
    end
    return best_uses >= required and best or nil, best_uses
end

local function perform_work_action(action, target_tag, tool_prefab)
    local player = G.ThePlayer
    if player == nil or not player:IsValid() then
        print("[JEV_DST_ACTION_ERROR]" .. string.lower(action.id) .. ":no_local_player")
        return
    end
    local inventory = player.replica ~= nil and player.replica.inventory or nil
    local controller = player.components ~= nil and player.components.playercontroller or nil
    if inventory == nil or controller == nil then
        print("[JEV_DST_ACTION_ERROR]" .. string.lower(action.id) .. ":controller_unavailable")
        return
    end

    local px, py, pz = player.Transform:GetWorldPosition()
    local candidates = G.TheSim:FindEntities(px, py, pz, 6, { target_tag }, EXCLUDE_TAGS)
    local target, required, tool, available, nearest_distance = nil, nil, nil, 0, nil
    for _, candidate in ipairs(candidates) do
        if candidate ~= player and candidate:IsValid() then
            local candidate_required = required_work(candidate, target_tag)
            local candidate_tool, candidate_available = nil, 0
            if candidate_required ~= nil then
                candidate_tool, candidate_available = find_sufficient_tool(inventory, tool_prefab, candidate_required)
            end
            local distance = player:GetDistanceSqToInst(candidate)
            if candidate_tool ~= nil and (target == nil or distance < nearest_distance) then
                target = candidate
                required = candidate_required
                tool = candidate_tool
                available = candidate_available
                nearest_distance = distance
            end
        end
    end
    if target == nil or required == nil or tool == nil then
        print("[JEV_DST_ACTION_ERROR]" .. string.lower(action.id) .. ":target_or_tool_unavailable")
        return
    end

    if player._jev_dst_work_task ~= nil then
        player._jev_dst_work_task:Cancel()
        player._jev_dst_work_task = nil
    end

    local tx, ty, tz = target.Transform:GetWorldPosition()
    player._jev_dst_work_sequence = (player._jev_dst_work_sequence or 0) + 1
    local work =
    {
        sequence = player._jev_dst_work_sequence,
        action = string.lower(action.id),
        target_guid = target.GUID,
        x = round(tx, 2),
        z = round(tz, 2),
        status = "started",
    }
    player._jev_dst_work = work

    local attempts = 0
    local function schedule(delay, fn)
        player._jev_dst_work_task = player:DoTaskInTime(delay, fn)
    end
    local function dispatch()
        player._jev_dst_work_task = nil
        if not player:IsValid() then
            return
        end
        if not target:IsValid() or not target:HasTag(target_tag) then
            work.status = "completed"
            print("[JEV_DST_ACTION]" .. string.lower(action.id) .. ":completed target=" .. tostring(target.GUID))
            return
        end
        if not tool:IsValid() then
            work.status = "failed"
            work.reason = "tool_broke_before_completion"
            print("[JEV_DST_ACTION_ERROR]" .. string.lower(action.id) .. ":tool_broke_before_completion")
            return
        end
        if safe_call(function() return controller:IsBusy() end, false) then
            schedule(0.15, dispatch)
            return
        end
        local buffaction = controller:GetActionButtonAction(target)
        if buffaction == nil or buffaction.action ~= action then
            schedule(0.2, dispatch)
            return
        end
        if not controller.ismastersim then
            if controller.locomotor == nil then
                buffaction.non_preview_cb = function()
                    controller:RemoteActionButton(buffaction, true)
                end
            else
                buffaction.preview_cb = function()
                    controller:RemoteActionButton(buffaction, true)
                end
            end
        end
        controller:DoAction(buffaction)
        attempts = attempts + 1
        if attempts > required + 5 then
            work.status = "failed"
            work.reason = "completion_timeout"
            print("[JEV_DST_ACTION_ERROR]" .. string.lower(action.id) .. ":completion_timeout")
            return
        end
        schedule(0.55, dispatch)
    end

    local equipped = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
    if equipped == nil or equipped.prefab ~= tool_prefab then
        inventory:UseItemFromInvTile(tool)
        schedule(0.3, dispatch)
    else
        dispatch()
    end
    print("[JEV_DST_ACTION]" .. string.lower(action.id) .. ":started target=" .. tostring(target.GUID)
        .. " required=" .. tostring(required) .. " available=" .. tostring(available))
end

local SAFE_FOOD_PRIORITY =
{
    "cookedmeat",
    "cookedsmallmeat",
    "berries_cooked",
    "carrot_cooked",
    "berries",
    "carrot",
    "seeds_cooked",
    "seeds",
}

local COOKABLE_FOOD_PRIORITY =
{
    "meat",
    "smallmeat",
    "berries",
    "carrot",
}

local function cook_one_food()
    local player = G.ThePlayer
    local inventory = player ~= nil and player.replica ~= nil and player.replica.inventory or nil
    local controller = player ~= nil and player.components ~= nil and player.components.playercontroller or nil
    if player == nil or not player:IsValid() or inventory == nil or controller == nil then
        print("[JEV_DST_ACTION_ERROR]cook_food:player_or_controller_unavailable")
        return
    end
    if safe_call(function() return controller:IsBusy() end, false) then
        print("[JEV_DST_ACTION_ERROR]cook_food:controller_busy")
        return
    end

    local food = nil
    for _, prefab in ipairs(COOKABLE_FOOD_PRIORITY) do
        food = inventory:FindItem(function(item)
            return item ~= nil and item:IsValid() and item.prefab == prefab
                and item:HasTag("cookable")
        end)
        if food ~= nil then
            break
        end
    end
    if food == nil then
        print("[JEV_DST_ACTION_ERROR]cook_food:no_cookable_food")
        return
    end

    local px, py, pz = player.Transform:GetWorldPosition()
    local fires = G.TheSim:FindEntities(px, py, pz, 6, { "cooker" }, EXCLUDE_TAGS)
    local fire, nearest_distance = nil, nil
    for _, candidate in ipairs(fires) do
        if candidate:IsValid() and (candidate.prefab == "campfire" or candidate.prefab == "firepit")
            and candidate:HasTag("fire") and not candidate:HasTag("fueldepleted") then
            local distance = player:GetDistanceSqToInst(candidate)
            if fire == nil or distance < nearest_distance then
                fire = candidate
                nearest_distance = distance
            end
        end
    end
    if fire == nil then
        print("[JEV_DST_ACTION_ERROR]cook_food:no_lit_cooker")
        return
    end

    local action = safe_call(function() return controller:GetItemUseAction(food, fire) end, nil)
    if action == nil or action.action ~= G.ACTIONS.COOK then
        print("[JEV_DST_ACTION_ERROR]cook_food:native_action_unavailable")
        return
    end
    if controller.ismastersim then
        controller:DoAction(action)
    else
        controller:RemoteControllerUseItemOnSceneFromInvTile(action, food)
    end
    print("[JEV_DST_ACTION]cook_food:requested prefab=" .. tostring(food.prefab)
        .. " fire=" .. tostring(fire.GUID))
end

local function eat_safe_food()
    local player = G.ThePlayer
    local inventory = player ~= nil and player.replica ~= nil and player.replica.inventory or nil
    if player == nil or not player:IsValid() or inventory == nil then
        print("[JEV_DST_ACTION_ERROR]eat_safe_food:player_or_inventory_unavailable")
        return
    end
    local food = nil
    for _, prefab in ipairs(SAFE_FOOD_PRIORITY) do
        food = inventory:FindItem(function(item)
            return item ~= nil and item:IsValid() and item.prefab == prefab
        end)
        if food ~= nil then
            break
        end
    end
    if food == nil then
        print("[JEV_DST_ACTION_ERROR]eat_safe_food:no_safe_food")
        return
    end
    inventory:UseItemFromInvTile(food)
    print("[JEV_DST_ACTION]eat_safe_food:requested prefab=" .. tostring(food.prefab))
end

local function attack_nearest_hostile()
    local player = G.ThePlayer
    local controller = player ~= nil and player.components ~= nil and player.components.playercontroller or nil
    if player == nil or not player:IsValid() or controller == nil then
        print("[JEV_DST_ACTION_ERROR]attack_nearest_hostile:controller_unavailable")
        return
    end
    local x, y, z = player.Transform:GetWorldPosition()
    local candidates = G.TheSim:FindEntities(x, y, z, 8, nil, EXCLUDE_TAGS)
    local target = nil
    local target_distance = nil
    for _, entity in ipairs(candidates) do
        if entity ~= player and entity:IsValid() and entity:HasAnyTag("hostile", "monster")
            and player.replica ~= nil and player.replica.combat ~= nil
            and safe_call(function() return player.replica.combat:CanTarget(entity) end, false) then
            local distance = player:GetDistanceSqToInst(entity)
            if target == nil or distance < target_distance then
                target = entity
                target_distance = distance
            end
        end
    end
    if target == nil then
        print("[JEV_DST_ACTION_ERROR]attack_nearest_hostile:no_hostile_target")
        return
    end
    -- Use the game's mouse-attack path. The second argument marks this as a
    -- primary/left-click attack, matching a player left-clicking the target.
    controller:DoAttackButton(target, true)
    print("[JEV_DST_ACTION]attack_nearest_hostile:left_click target=" .. tostring(target.GUID))
end

local function build_campfire()
    local player = G.ThePlayer
    local builder = player ~= nil and player.replica ~= nil and player.replica.builder or nil
    local recipe = G.GetValidRecipe("campfire")
    if player == nil or not player:IsValid() or builder == nil or recipe == nil then
        print("[JEV_DST_ACTION_ERROR]build_campfire:builder_unavailable")
        return
    end
    if G.TheWorld == nil or not G.TheWorld.state.isnight then
        print("[JEV_DST_ACTION_ERROR]build_campfire:not_night")
        return
    end
    if has_nearby_lit_fire(player, 8) then
        print("[JEV_DST_ACTION_ERROR]build_campfire:lit_fire_nearby")
        return
    end
    local already_buffered = builder:IsBuildBuffered(recipe.name)
    if builder:IsBusy() or (not already_buffered and not builder:CanBuild("campfire")) then
        print("[JEV_DST_ACTION_ERROR]build_campfire:not_craftable_or_busy")
        return
    end

    local controller = player.components ~= nil and player.components.playercontroller or nil
    if controller == nil then
        print("[JEV_DST_ACTION_ERROR]build_campfire:controller_unavailable")
        return
    end

    -- Match DoRecipeClick for recipes with placers: first buffer/craft the
    -- structure, then enter placement mode. MakeRecipeAtPoint alone is
    -- rejected by the server when no buffered campfire exists.
    if not already_buffered then
        builder:BufferBuild(recipe.name)
    end
    if not builder:IsBuildBuffered(recipe.name) then
        print("[JEV_DST_ACTION_ERROR]build_campfire:buffer_failed")
        return
    end
    controller:StartBuildPlacementMode(recipe, nil)
    print("[JEV_DST_ACTION]build_campfire:buffered")

    player:DoTaskInTime(0.35, function()
        if not player:IsValid() or not builder:IsBuildBuffered(recipe.name) then
            print("[JEV_DST_ACTION_ERROR]build_campfire:buffer_lost_before_placement")
            return
        end

        local px, py, pz = player.Transform:GetWorldPosition()
        local point = nil
        local radii = { 1.25, 1.5, 1.75, 2.0 }
        for _, radius in ipairs(radii) do
            for angle = 0, 315, 45 do
                local radians = angle * G.DEGREES
                local candidate = G.Vector3(px + math.cos(radians) * radius, 0, pz + math.sin(radians) * radius)
                if builder:CanBuildAtPoint(candidate, recipe, 0) then
                    point = candidate
                    break
                end
            end
            if point ~= nil then
                break
            end
        end
        if point == nil then
            controller:CancelPlacement()
            print("[JEV_DST_ACTION_ERROR]build_campfire:no_valid_position")
            return
        end

        if controller.placer ~= nil then
            controller.placer.Transform:SetPosition(point.x, point.y, point.z)
            controller.placer.Transform:SetRotation(0)
        end
        builder:MakeRecipeAtPoint(recipe, point, 0)
        controller:CancelPlacement()
        print("[JEV_DST_ACTION]build_campfire:placed x=" .. tostring(round(point.x, 2))
            .. " z=" .. tostring(round(point.z, 2)))
    end)
end

local function build_science_machine()
    local player = G.ThePlayer
    local builder = player ~= nil and player.replica ~= nil and player.replica.builder or nil
    local recipe = G.GetValidRecipe("sciencemachine")
    if player == nil or not player:IsValid() or builder == nil or recipe == nil then
        print("[JEV_DST_ACTION_ERROR]build_science_machine:builder_unavailable")
        return
    end
    if builder:IsBusy() or not builder:CanBuild("sciencemachine") then
        print("[JEV_DST_ACTION_ERROR]build_science_machine:not_craftable_or_busy")
        return
    end

    local controller = player.components ~= nil and player.components.playercontroller or nil
    if controller == nil then
        print("[JEV_DST_ACTION_ERROR]build_science_machine:controller_unavailable")
        return
    end

    builder:BufferBuild(recipe.name)
    if not builder:IsBuildBuffered(recipe.name) then
        print("[JEV_DST_ACTION_ERROR]build_science_machine:buffer_failed")
        return
    end
    controller:StartBuildPlacementMode(recipe, nil)
    print("[JEV_DST_ACTION]build_science_machine:buffered")

    player:DoTaskInTime(0.35, function()
        if not player:IsValid() or not builder:IsBuildBuffered(recipe.name) then
            print("[JEV_DST_ACTION_ERROR]build_science_machine:buffer_lost_before_placement")
            return
        end

        local px, py, pz = player.Transform:GetWorldPosition()
        local point = nil
        local radii = { 1.25, 1.5, 1.75, 2.0 }
        for _, radius in ipairs(radii) do
            for angle = 0, 315, 45 do
                local radians = angle * G.DEGREES
                local candidate = G.Vector3(px + math.cos(radians) * radius, 0, pz + math.sin(radians) * radius)
                if builder:CanBuildAtPoint(candidate, recipe, 0) then
                    point = candidate
                    break
                end
            end
            if point ~= nil then
                break
            end
        end
        if point == nil then
            controller:CancelPlacement()
            print("[JEV_DST_ACTION_ERROR]build_science_machine:no_valid_position")
            return
        end

        if controller.placer ~= nil then
            controller.placer.Transform:SetPosition(point.x, point.y, point.z)
            controller.placer.Transform:SetRotation(0)
        end
        builder:MakeRecipeAtPoint(recipe, point, 0)
        controller:CancelPlacement()
        print("[JEV_DST_ACTION]build_science_machine:placed x=" .. tostring(round(point.x, 2))
            .. " z=" .. tostring(round(point.z, 2)))
    end)
end

local function equip_torch()
    local player = G.ThePlayer
    if player == nil or not player:IsValid() then
        print("[JEV_DST_ACTION_ERROR]equip_torch:no_local_player")
        return
    end
    if G.TheWorld == nil or not G.TheWorld.state.isnight then
        print("[JEV_DST_ACTION_ERROR]equip_torch:not_night")
        return
    end
    if has_nearby_lit_fire(player, 8) then
        print("[JEV_DST_ACTION_ERROR]equip_torch:lit_fire_nearby")
        return
    end

    local inventory = player.replica ~= nil and player.replica.inventory or nil
    if inventory == nil then
        print("[JEV_DST_ACTION_ERROR]equip_torch:inventory_unavailable")
        return
    end

    local torch = inventory:FindItem(function(item)
        return item ~= nil and item:IsValid() and item.prefab == "torch"
    end)
    if torch == nil then
        print("[JEV_DST_ACTION_ERROR]equip_torch:no_torch")
        return
    end

    -- Mirror a player clicking the item in an inventory tile. EquipActionItem
    -- is an action-system auto-equip helper and does not directly equip here.
    inventory:UseItemFromInvTile(torch)
    print("[JEV_DST_ACTION]equip_torch:requested guid=" .. tostring(torch.GUID))
end

local function unequip_torch()
    local player = G.ThePlayer
    if player == nil or not player:IsValid() then
        print("[JEV_DST_ACTION_ERROR]unequip_torch:no_local_player")
        return
    end
    if G.TheWorld ~= nil and G.TheWorld.state.isnight and not has_nearby_lit_fire(player, 8) then
        print("[JEV_DST_ACTION_ERROR]unequip_torch:no_alternate_light")
        return
    end

    local inventory = player.replica ~= nil and player.replica.inventory or nil
    if inventory == nil then
        print("[JEV_DST_ACTION_ERROR]unequip_torch:inventory_unavailable")
        return
    end

    local hands_item = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
    if hands_item == nil or hands_item.prefab ~= "torch" then
        print("[JEV_DST_ACTION_ERROR]unequip_torch:torch_not_equipped")
        return
    end

    inventory:TakeActiveItemFromEquipSlot(G.EQUIPSLOTS.HANDS)
    player:DoTaskInTime(0.1, function()
        if player:IsValid() and player.replica ~= nil and player.replica.inventory ~= nil then
            player.replica.inventory:ReturnActiveItem()
        end
    end)
    print("[JEV_DST_ACTION]unequip_torch:requested guid=" .. tostring(hands_item.GUID))
end

local function equip_best_weapon()
    local player = G.ThePlayer
    local inventory = player ~= nil and player.replica ~= nil and player.replica.inventory or nil
    if player == nil or not player:IsValid() or inventory == nil then
        print("[JEV_DST_ACTION_ERROR]equip_weapon:player_or_inventory_unavailable")
        return
    end

    local equipped = inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
    if equipped ~= nil and equipped.prefab ~= "torch"
        and (equipped:HasTag("weapon") or WEAPON_PREFAB_SCORE[equipped.prefab or ""] ~= nil) then
        print("[JEV_DST_ACTION]equip_weapon:already_equipped guid=" .. tostring(equipped.GUID))
        return
    end

    local best = nil
    local best_score = nil
    local items = safe_call(function() return inventory:GetItems() end, {}) or {}
    for _, item in pairs(items) do
        if item ~= nil and item:IsValid() and item.prefab ~= "torch"
            and (item:HasTag("weapon") or WEAPON_PREFAB_SCORE[item.prefab or ""] ~= nil) then
            local score = WEAPON_PREFAB_SCORE[item.prefab or ""] or 1
            if item.components ~= nil and item.components.weapon ~= nil then
                score = safe_call(function() return item.components.weapon:GetDamage(player, nil) end, score)
            end
            if best == nil or score > best_score then
                best = item
                best_score = score
            end
        end
    end
    if best == nil then
        print("[JEV_DST_ACTION_ERROR]equip_weapon:no_weapon")
        return
    end

    inventory:UseItemFromInvTile(best)
    print("[JEV_DST_ACTION]equip_weapon:requested prefab=" .. tostring(best.prefab)
        .. " guid=" .. tostring(best.GUID))
end

-- Reserved bridge keys. The external controller emits these keys; the mod
-- translates them into bounded semantic actions and validates prerequisites.
G.TheInput:AddKeyUpHandler(G.KEY_KP_MULTIPLY, craft_torch)
G.TheInput:AddKeyUpHandler(G.KEY_KP_DIVIDE, equip_torch)
G.TheInput:AddKeyUpHandler(G.KEY_KP_MINUS, unequip_torch)
G.TheInput:AddKeyUpHandler(G.KEY_KP_PLUS, function() craft_item("axe") end)
G.TheInput:AddKeyUpHandler(G.KEY_KP_PERIOD, function() craft_item("pickaxe") end)
G.TheInput:AddKeyUpHandler(G.KEY_F6, build_campfire)
G.TheInput:AddKeyUpHandler(G.KEY_F7, eat_safe_food)
G.TheInput:AddKeyUpHandler(G.KEY_F8, function() perform_work_action(G.ACTIONS.CHOP, "CHOP_workable", "axe") end)
G.TheInput:AddKeyUpHandler(G.KEY_F9, function() perform_work_action(G.ACTIONS.MINE, "MINE_workable", "pickaxe") end)
G.TheInput:AddKeyUpHandler(G.KEY_F10, attack_nearest_hostile)
G.TheInput:AddKeyUpHandler(G.KEY_F11, equip_best_weapon)
G.TheInput:AddKeyUpHandler(G.KEY_F12, build_science_machine)
G.TheInput:AddKeyUpHandler(G.KEY_F5, reject_current_frontier)
G.TheInput:AddKeyUpHandler(G.KEY_F4, reject_current_road_target)
G.TheInput:AddKeyUpHandler(G.KEY_F3, cook_one_food)

-- ThePlayer is often still nil while player prefabs are being initialized on a
-- joining client. Poll from the world instead of making a one-shot comparison.
-- A world task is also cleaned up automatically when leaving the world.
local function try_start_telemetry(player)
    if player == nil or not player:IsValid() then
        return
    end
    if player ~= G.ThePlayer then
        return
    end
    if player._jev_dst_started then
        return
    end
    player._jev_dst_started = true
    print("[JEV_DST]local_player_ready prefab=" .. tostring(player.prefab))
    start_telemetry(player)
end

AddPlayerPostInit(function(player)
    if player == nil then
        return
    end
    player:ListenForEvent("playeractivated", function()
        try_start_telemetry(player)
    end)
end)

AddSimPostInit(function()
    local world = G.TheWorld
    if world == nil or not world:IsValid() then
        return
    end
    world:DoTaskInTime(1.0, function()
        try_start_telemetry(G.ThePlayer)
    end)
end)
