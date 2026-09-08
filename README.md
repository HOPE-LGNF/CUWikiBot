# CUWikiBot

CUWikiBot 是一个基于 [HiklQQBot](https://github.com/kldhsh123/hiklqqbot/) 的 QQ 机器人，用于便捷查询[未知伤亡中文维基](https://casualtiesunknown.huijiwiki.com)的相关编写进度。

> 关于维基编辑者交流群，请见未知伤亡中文维基站内。

## 当前定位与已知局限

当前项目以 WebSocket 模式下的简单群聊命令接收和文本回复为主要可用范围，适合在现有 HiklQQBot 基础上继续开发轻量功能。建议使用 `COMM_MODE=websocket`；程序目前仍要求同时配置 `BOT_APPID`、`BOT_APPSECRET` 和非空的 `BOT_TOKEN`。

以下能力尚未作为可用范围：

- 群消息中的用户身份字段尚未完全适配当前 QQ 事件格式，因此依赖用户身份的管理员初始化、用户黑名单、用户统计和指定用户按钮权限可能无法正常工作。
- 频道消息、频道私信及对应事件订阅尚未完整适配当前 QQ API。
- Webhook 模式尚未完整实现普通回调的签名校验和协议 ACK，不应直接用于正式环境。
- `plugins/cu_stats.py` 已完成一次目标站点的公开基础统计与短页面首批查询验证；待巡查统计尚未实现，短页面只展示已读取的列表条数，长期访问稳定性及真实 QQ 统计消息发送仍未验收。
- Markdown、按钮、富媒体和主动消息还会受到 QQ 开放平台权限及频率限制，尚未完成真实账号下的全量验收。

当前开发应优先保持简单群聊功能可用。若项目需要扩展到多账号、高并发、完整频道/Webhook 支持或长期生产运维，应先评估迁移到持续维护的正式 QQ Bot 框架，避免继续扩大对现有框架内部实现的依赖。

当前业务插件的数据流、指标口径和迁移边界见 [PLUGIN_ARCHITECTURE.md](PLUGIN_ARCHITECTURE.md)。

## Wiki 统计查询

发送 `/wiki统计` 查询固定 Wiki，不接受自定义 URL。API 地址、站点名、指纹、超时和读取上限集中在 `plugins/cu_stats.py` 顶部，适合当前少量用户直接维护。

Wiki 访问采用 `curl-cffi` 的 `chrome131` 浏览器指纹，通过同一个异步 Session 向 `/api.php` 发送 POST 请求并自动复用 Cookie。会话贯穿一次统计任务，结束后关闭；公开查询无需登录，可在 `.env` 配置 `WIKI_USERNAME` 和 `WIKI_PASSWORD` 启用 Wiki 登录。这与 QQ 凭据无关。登录失败会明确提示，并继续尝试公开统计；不自动处理验证码、二次验证或外部登录跳转。单请求默认 15 秒，整个网络任务默认最多 60 秒，不自动重试。此方案不执行五秒盾的 JavaScript 验证，不能保证长期绕过防护。

短页面最多读取首批 500 条，有后续时会明确提示；它不是 170 字节阈值的全站统计。`mwclient + mwparserfromhell` 的可行性、实测结果和暂缓引入理由也记录在架构文档中。

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

## 开发检查

普通 `uv sync` 会同时安装开发依赖中的 Ruff 和 ty。默认检查范围仅包含当前实际维护的业务模块 `plugins/cu_stats.py`：

```bash
uv run ruff format .
uv run ruff check . --preview
uv run ty check
uv run python -m unittest discover -s tests
```

Ruff 和 ty 的范围及格式规则位于 `pyproject.toml`。VS Code 打开仓库后安装推荐的 Ruff 扩展，Python 文件会在保存时格式化；PyCharm 选择 `.venv` 作为项目解释器并启用 Ruff 插件，即会复用同一份配置。机器人框架计划替换，不纳入当前质量基线，因此暂未把全仓检查设为 pre-commit 门禁。

原始 HiklQQBot 文档保存在 [Original_README.md](Original_README.md)。
