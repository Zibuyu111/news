# my_news

个人技术文章定时汇总脚本。当前第一版接入 Hacker News 官方 API，按关键词和分数筛选文章，生成纯文本与 HTML 预览；配置邮箱后可以定时发送邮件。

## 本地测试

```bash
python3 main.py --dry-run --ignore-db --limit 10
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
- `filters.min_score`：最低 HN 分数
- `hacker_news.timeout`：HN API 单次请求超时时间
- `hacker_news.retries`：HN API 请求失败后的重试次数
- `email.enabled`：是否启用邮件发送

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

cron 示例，每天 08:00 执行：

```cron
0 8 * * * cd /path/to/my_news && /usr/bin/python3 main.py >> /path/to/my_news/out/cron.log 2>&1
```
