# JEV DST Agent

[English](README.EN.md) · [技术说明](docs/TECHNICAL.md)

让 JEV 模型在《饥荒联机版》中根据实时状态选择动作的实验项目。客户端 Mod 读取游戏状态，Python 控制器执行有限的游戏操作；可选用 DeepSeek 定期生成计划，供 JEV 决策时参考。当前目标是制作炼金引擎。

已完成的阶段：[活过第一晚](https://bilibili.com/video/BV1aehE6DEji) · [制作科学机器](https://www.bilibili.com/video/BV1mFaV6aEQ2)

## 快速开始

1. 将整个 `jev_dst_agent` 文件夹复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **JEV DST Agent**，然后进入本地或私人世界。
2. 将 `.env.example` 复制为 `.env`，填写 `JEV_API_KEY`。如需启用规划，再填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 在项目文件夹中打开 PowerShell，先查看状态和单次决策，再启动控制循环：

```powershell
python.exe .\watch_state.py
python.exe .\jev_agent.py --show-probabilities
python.exe .\jev_agent.py --loop --execute
```

最后一条命令会向游戏发送实际操作；按 `Ctrl+C` 停止。若只想观察决策而不操作游戏，去掉 `--execute`。

## 测试与文档

无需启动游戏即可运行离线回归测试：

```powershell
python.exe .\run_tests.py
```

状态格式、动作规则、候选裁剪、探索导航与规划机制见[技术说明](docs/TECHNICAL.md)。规划用的游戏知识可以在 [DSTKnowledge.txt](DSTKnowledge.txt) 中编辑。

灵感来自 [TerraBlind](https://github.com/Reisenbug/TerraBlind)。
