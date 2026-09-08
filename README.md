# CUWikiBot

CUWikiBot 是一个基于 [HiklQQBot](https://github.com/kldhsh123/hiklqqbot/) 的 QQ 机器人，用于便捷查询[未知伤亡中文维基](https://casualtiesunknown.huijiwiki.com)的相关编写进度。

> 关于维基编辑者交流群，请见未知伤亡中文维基站内。

## 当前定位与已知局限

当前项目以 WebSocket 模式下的简单群聊命令接收和文本回复为主要可用范围，适合在现有 HiklQQBot 基础上继续开发轻量功能。建议使用 `COMM_MODE=websocket`；程序目前仍要求同时配置 `BOT_APPID`、`BOT_APPSECRET` 和非空的 `BOT_TOKEN`。

以下能力尚未作为可用范围：

- 群消息中的用户身份字段尚未完全适配当前 QQ 事件格式，因此依赖用户身份的管理员初始化、用户黑名单、用户统计和指定用户按钮权限可能无法正常工作。
- 频道消息、频道私信及对应事件订阅尚未完整适配当前 QQ API。
- Webhook 模式尚未完整实现普通回调的签名校验和协议 ACK，不应直接用于正式环境。
- `plugins/cu_stats.py` 的 MediaWiki API 查询存在已知问题，不属于当前 QQ 机器人基础链路的可用性保证。
- Markdown、按钮、富媒体和主动消息还会受到 QQ 开放平台权限及频率限制，尚未完成真实账号下的全量验收。

当前开发应优先保持简单群聊功能可用。若项目需要扩展到多账号、高并发、完整频道/Webhook 支持或长期生产运维，应先评估迁移到持续维护的正式 QQ Bot 框架，避免继续扩大对现有框架内部实现的依赖。

当前业务插件的数据流、指标口径和迁移边界见 [PLUGIN_ARCHITECTURE.md](PLUGIN_ARCHITECTURE.md)。

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
