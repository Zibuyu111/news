# my_news

个人技术文章定时汇总脚本。当前接入 Hacker News 与多个公开 RSS/Atom 技术源，按关键词、热度、时间和来源均衡筛选文章，生成纯文本与 HTML 预览；配置邮箱后可以定时发送邮件。

## 本地测试

```bash
python3 main.py --dry-run --ignore-db --limit 12
```

`--dry-run` 只生成预览，不发送邮件，也不写入已发送记录。

当前版本只使用 Python 标准库，不强制要求安装第三方依赖。建议使用 Python 3.11+。

输出文件位于：

```text
out/
```

## 配置

主要配置在 `config.toml`：

- `app.limit`：每次推送文章数量
- `app.scan_count`：从 Hacker News 热门列表中扫描多少条
- `filters.fill_with_top`：关键词命中不足时，是否用 HN 高分文章补齐
- `filters.keywords`：关键词列表
- `filters.min_score`：最低 HN 分数，仅作用于有分数的来源
- `filters.max_per_source`：单个来源最多入选多少条
- `feeds.enabled`：是否启用 RSS/Atom 来源
- `feeds.sources`：公开 RSS/Atom 来源列表
- `hacker_news.timeout`：HN API 单次请求超时时间
- `hacker_news.retries`：HN API 请求失败后的重试次数
- `email.enabled`：是否启用邮件发送

默认启用的 RSS/Atom 来源包括：

- Lobsters
- GitHub Blog
- AWS Open Source
- OpenAI News
- Rust Blog
- Go Blog
- Kubernetes Blog
- Mozilla Hacks
- Meta Engineering
- CNCF Blog

`config.toml` 中还保留了 Cloudflare Blog，当前默认关闭；如果本地网络访问稳定，可以把对应来源的 `enabled = false` 改为 `enabled = true`。

## 筛选逻辑

脚本会先抓取 Hacker News 和启用的 RSS/Atom 来源，再按 URL 去重。筛选时：

- Hacker News 等有热度数据的来源会应用 `filters.min_score`
- RSS/Atom 来源没有统一分数，不受 `min_score` 限制
- 标题、链接和摘要会参与关键词匹配
- `filters.priority_sources` 中的来源会优先保留
- `filters.max_per_source` 控制单个来源最多入选多少条，避免某个站点占满摘要

## 添加来源

在 `config.toml` 中追加一个 RSS/Atom 来源即可：

```toml
[[feeds.sources]]
name = "Example Blog"
url = "https://example.com/feed.xml"
```

如果某个来源偶尔超时但仍想保留配置，可以设置：

```toml
enabled = false
```

之后需要启用时改回 `true` 或删除这一行。

SMTP 密码不要写入配置文件，使用环境变量：

```bash
export NEWS_SMTP_PASSWORD="你的 SMTP 授权码"
```

## 发送邮件

先在 `config.toml` 中填写：

```toml
[email]
enabled = true
sender = "你的邮箱"
receiver = "接收邮箱"
```

然后运行：

```bash
python3 main.py
```

成功发送后，文章 URL 会写入 SQLite，后续运行不会重复推送。

## 定时运行

建议使用仓库里的 `run_daily.sh`，它会读取同目录 `.env` 中的 SMTP 授权码，并优先使用 `python3.11`。

cron 示例，每天 08:00 执行：

```cron
CRON_TZ=Asia/Shanghai
0 8 * * * /path/to/my_news/run_daily.sh >> /path/to/my_news/out/cron.log 2>&1
```
