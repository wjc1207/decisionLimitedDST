# JEV DST Agent

[中文版](README.md) · [Technical reference](docs/TECHNICAL.EN.md)

An experimental agent for Don't Starve Together: a client mod reports game state, JEV selects actions, and a Python controller performs a bounded set of game inputs. Optional DeepSeek plans provide longer-term context for JEV's decisions. The current milestone is to build an Alchemy Engine.

Previous milestones: [survive the first night](https://bilibili.com/video/BV1aehE6DEji) · [build a Science Machine](https://www.bilibili.com/video/BV1mFaV6aEQ2)

## Quick start

1. Copy the entire `jev_dst_agent` folder into Don't Starve Together's `mods` directory. Enable **JEV DST Agent** in the game's Mods menu and enter a local or private world.
2. Copy `.env.example` to `.env` and fill in `JEV_API_KEY`. Add `DEEPSEEK_API_KEY` if you want periodic planning. Do not share or commit `.env`.
3. Open PowerShell in the project folder. Inspect live state and a single decision, then start the control loop:

```powershell
python.exe .\watch_state.py
python.exe .\jev_agent.py --show-probabilities
python.exe .\jev_agent.py --loop --execute
```

The last command sends real game inputs; press `Ctrl+C` to stop. Omit `--execute` to observe decisions without controlling the game.

## Tests and documentation

Run offline regression tests without launching the game:

```powershell
python.exe .\run_tests.py
```

See the [technical reference](docs/TECHNICAL.EN.md) for state data, action rules, candidate pruning, exploration and planning. Edit [DSTKnowledge.txt](DSTKnowledge.txt) to change the game knowledge supplied to the planner.

Inspired by [TerraBlind](https://github.com/Reisenbug/TerraBlind).
