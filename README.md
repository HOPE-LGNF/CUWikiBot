# CUWikiBot

CUWikiBot 是一个基于 [HiklQQBot](https://github.com/kldhsh123/hiklqqbot/) 的 QQ 机器人，用于便捷查询[未知伤亡中文维基](https://casualtiesunknown.huijiwiki.com)的相关编写进度。

> 关于维基编辑者交流群，请见未知伤亡中文维基站内。

## 环境与运行

项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 版本、依赖和虚拟环境。首次运行时执行：

```bash
uv sync
cp .env.example .env
```

填写 `.env` 中的机器人凭据后启动：

```bash
uv run python main.py
```

依赖以 `pyproject.toml` 和 `uv.lock` 为准。添加或移除依赖时使用：

```bash
uv add <package>
uv remove <package>
```

部署已有锁文件的版本时可执行 `uv sync --locked`，确保环境与锁文件一致。

原始 HiklQQBot 文档保存在 [Original_README.md](Original_README.md)。
