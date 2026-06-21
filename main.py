#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import smtplib
import sqlite3
import textwrap
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


HN_API = "https://hacker-news.firebaseio.com/v0"
HN_ALGOLIA_API = "https://hn.algolia.com/api/v1"
HN_WEB = "https://news.ycombinator.com/"


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    discussion_url: str
    score: int = 0
    comments: int = 0
    published_at: int | None = None
    matched: bool = True


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def ensure_dirs(config: dict, base_dir: Path) -> None:
    db_path = base_dir / config["app"]["database"]
    out_dir = base_dir / config["app"]["output_dir"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)


def open_db(config: dict, base_dir: Path) -> sqlite3.Connection:
    db_path = base_dir / config["app"]["database"]
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_articles (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def already_sent(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT 1 FROM sent_articles WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_sent(conn: sqlite3.Connection, articles: Iterable[Article], now: datetime) -> None:
    rows = [(a.url, a.title, a.source, now.isoformat()) for a in articles]
    conn.executemany(
        """
        INSERT OR IGNORE INTO sent_articles (url, title, source, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def keyword_score(article: Article, keywords: list[str]) -> int:
    text = f"{article.title} {article.url}".lower()
    return sum(1 for keyword in keywords if keyword_matches(text, keyword))


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower().strip()
    if not keyword:
        return False

    if re.search(r"\W", keyword):
        return keyword in text

    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def is_relevant(article: Article, config: dict) -> bool:
    min_score = int(config["filters"].get("min_score", 0))
    keywords = config["filters"].get("keywords", [])

    if article.score < min_score:
        return False

    if not keywords:
        return True

    return keyword_score(article, keywords) > 0


def fetch_hacker_news(config: dict) -> list[Article]:
    hn_config = config.get("hacker_news", {})
    if not hn_config.get("enabled", True):
        return []

    fetchers = {
        "firebase": ("Firebase API", fetch_hacker_news_api),
        "algolia": ("Algolia API", fetch_hacker_news_algolia),
        "html": ("HTML", fetch_hacker_news_html),
    }
    preferred = str(hn_config.get("api", "firebase")).lower()
    order = [preferred, "firebase", "algolia", "html"]

    errors = []
    for name in dict.fromkeys(order):
        if name not in fetchers:
            continue

        label, fetcher = fetchers[name]
        try:
            return fetcher(config)
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{label}: {exc}")
            print(f"Hacker News {label} failed: {exc}")

    raise RuntimeError("All Hacker News fetch methods failed: " + " | ".join(errors))


def fetch_hacker_news_api(config: dict) -> list[Article]:
    hn_config = config.get("hacker_news", {})
    list_name = hn_config.get("list", "topstories")
    scan_count = int(config["app"].get("scan_count", 100))
    timeout = int(hn_config.get("timeout", 30))

    item_ids = get_json(f"{HN_API}/{list_name}.json", config=config, timeout=timeout)
    if not isinstance(item_ids, list):
        raise RuntimeError(f"unexpected {list_name} payload")

    articles: list[Article] = []
    for item_id in item_ids[:scan_count]:
        try:
            item = get_json(f"{HN_API}/item/{item_id}.json", config=config, timeout=timeout)
        except RuntimeError as exc:
            print(f"Skip HN item {item_id}: {exc}")
            continue
        if not item or item.get("type") != "story":
            continue

        title = html.unescape(item.get("title", "")).strip()
        if not title:
            continue

        discussion_url = f"https://news.ycombinator.com/item?id={item_id}"
        url = item.get("url") or discussion_url

        articles.append(
            Article(
                source="Hacker News",
                title=title,
                url=url,
                discussion_url=discussion_url,
                score=int(item.get("score", 0)),
                comments=int(item.get("descendants", 0)),
                published_at=item.get("time"),
            )
        )

    return articles


def fetch_hacker_news_algolia(config: dict) -> list[Article]:
    hn_config = config.get("hacker_news", {})
    scan_count = int(config["app"].get("scan_count", 100))
    timeout = int(hn_config.get("timeout", 30))
    params = urllib.parse.urlencode({"tags": "front_page", "hitsPerPage": scan_count})

    payload = get_json(f"{HN_ALGOLIA_API}/search?{params}", config=config, timeout=timeout)
    hits = payload.get("hits", [])
    if not isinstance(hits, list):
        raise RuntimeError("unexpected Algolia payload")

    articles: list[Article] = []
    for hit in hits[:scan_count]:
        title = html.unescape(str(hit.get("title") or hit.get("story_title") or "")).strip()
        if not title:
            continue

        object_id = str(hit.get("objectID") or hit.get("story_id") or "").strip()
        discussion_url = f"https://news.ycombinator.com/item?id={object_id}" if object_id else HN_WEB
        url = str(hit.get("url") or hit.get("story_url") or "").strip() or discussion_url

        articles.append(
            Article(
                source="Hacker News",
                title=title,
                url=url,
                discussion_url=discussion_url,
                score=as_int(hit.get("points")),
                comments=as_int(hit.get("num_comments")),
                published_at=as_int(hit.get("created_at_i")) or None,
            )
        )

    return articles


class HackerNewsHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.articles: list[dict] = []
        self._current_story: dict | None = None
        self._capture_title = False
        self._capture_score = False
        self._capture_comments = False
        self._current_link = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        class_names = attr.get("class", "") or ""

        if tag == "tr" and "athing" in class_names:
            self._current_story = {"id": attr.get("id", ""), "title": "", "url": "", "score": 0, "comments": 0}
            return

        if self._current_story is not None and tag == "a" and "titleline" not in class_names:
            # HN wraps story links in <span class="titleline"><a ...>.
            href = attr.get("href")
            if href and not self._current_story["url"]:
                self._capture_title = True
                self._current_link = href
                self._current_text = []
            return

        if tag == "span" and "score" in class_names:
            self._capture_score = True
            self._current_text = []
            return

        if tag == "a":
            href = attr.get("href", "") or ""
            if href.startswith("item?id="):
                self._capture_comments = True
                self._current_link = href
                self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title and self._current_story is not None:
            title = html.unescape("".join(self._current_text).strip())
            if title:
                self._current_story["title"] = title
                self._current_story["url"] = normalize_hn_url(self._current_link)
                self.articles.append(self._current_story)
            self._capture_title = False
            self._current_text = []
            return

        if tag == "span" and self._capture_score:
            text = "".join(self._current_text).strip()
            score = parse_first_int(text)
            if self.articles:
                self.articles[-1]["score"] = score
            self._capture_score = False
            self._current_text = []
            return

        if tag == "a" and self._capture_comments:
            text = "".join(self._current_text).strip().lower()
            if "comment" in text and self.articles:
                self.articles[-1]["comments"] = parse_first_int(text)
            self._capture_comments = False
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._capture_title or self._capture_score or self._capture_comments:
            self._current_text.append(data)


def fetch_hacker_news_html(config: dict) -> list[Article]:
    timeout = int(config.get("hacker_news", {}).get("timeout", 30))
    html_text = get_text(HN_WEB, config=config, timeout=timeout)
    parser = HackerNewsHTMLParser()
    parser.feed(html_text)

    articles: list[Article] = []
    for item in parser.articles[: int(config["app"].get("scan_count", 100))]:
        item_id = item.get("id") or ""
        discussion_url = f"https://news.ycombinator.com/item?id={item_id}" if item_id else HN_WEB
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip() or discussion_url
        if not title:
            continue

        articles.append(
            Article(
                source="Hacker News",
                title=title,
                url=url,
                discussion_url=discussion_url,
                score=int(item.get("score", 0)),
                comments=int(item.get("comments", 0)),
            )
        )

    return articles


def normalize_hn_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://news.ycombinator.com/{url.lstrip('/')}"


def parse_first_int(text: str) -> int:
    digits = []
    for char in text:
        if char.isdigit():
            digits.append(char)
        elif digits:
            break
    return int("".join(digits)) if digits else 0


def as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_json(url: str, config: dict, timeout: int = 15):
    payload = get_text(url, config=config, timeout=timeout, accept="application/json")
    return json.loads(payload)


def get_text(url: str, config: dict, timeout: int = 15, accept: str = "text/html") -> str:
    retries = int(config.get("hacker_news", {}).get("retries", 1))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return request_text_once(url, timeout=timeout, accept=accept)
        except RuntimeError as exc:
            last_error = exc
            if attempt < retries:
                print(f"Retry {attempt}/{retries} for {url}: {exc}")

    raise RuntimeError(str(last_error))


def request_text_once(url: str, timeout: int = 15, accept: str = "text/html") -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "my-news-digest/0.1 (+local personal digest)",
            "Accept": accept,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset)
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"request failed: {url}: {exc}") from exc


def select_articles(
    articles: list[Article],
    config: dict,
    conn: sqlite3.Connection | None,
    limit: int,
    ignore_db: bool,
) -> list[Article]:
    candidates = [article for article in articles if article.score >= int(config["filters"].get("min_score", 0))]

    if conn is not None and not ignore_db:
        candidates = [article for article in candidates if not already_sent(conn, article.url)]

    strong = [article for article in candidates if is_relevant(article, config)]
    weak = [article for article in candidates if article not in strong]

    strong.sort(
        key=lambda article: (
            keyword_score(article, config["filters"].get("keywords", [])),
            article.score,
            article.comments,
        ),
        reverse=True,
    )

    selected = strong[:limit]
    if len(selected) < limit and config["filters"].get("fill_with_top", True):
        weak.sort(key=lambda article: (article.score, article.comments), reverse=True)
        selected.extend(
            Article(
                source=article.source,
                title=article.title,
                url=article.url,
                discussion_url=article.discussion_url,
                score=article.score,
                comments=article.comments,
                published_at=article.published_at,
                matched=False,
            )
            for article in weak[: limit - len(selected)]
        )

    return selected[:limit]


def render_text(articles: list[Article], config: dict, now: datetime) -> str:
    lines = [
        f"{config['app'].get('title', '技术文章自动汇总')} | {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if not articles:
        lines.append("本次没有筛选到新的文章。")
        return "\n".join(lines)

    for index, article in enumerate(articles, start=1):
        lines.extend(
            [
                f"{index}. [{article.source}{'' if article.matched else ' / 高分补位'}] {article.title}",
                f"   链接：{article.url}",
                f"   讨论：{article.discussion_url}",
                f"   HN：{article.score} points / {article.comments} comments",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def render_html(articles: list[Article], config: dict, now: datetime) -> str:
    title = html.escape(config["app"].get("title", "技术文章自动汇总"))
    timestamp = html.escape(now.strftime("%Y-%m-%d %H:%M"))

    if not articles:
        body = "<p>本次没有筛选到新的文章。</p>"
    else:
        items = []
        for index, article in enumerate(articles, start=1):
            items.append(
                textwrap.dedent(
                    f"""
                    <li>
                      <p><strong>{index}. [{html.escape(article.source)}{'' if article.matched else ' / 高分补位'}] {html.escape(article.title)}</strong></p>
                      <p><a href="{html.escape(article.url)}">原文链接</a> |
                         <a href="{html.escape(article.discussion_url)}">HN 讨论</a></p>
                      <p>{article.score} points / {article.comments} comments</p>
                    </li>
                    """
                ).strip()
            )
        body = "<ol>\n" + "\n".join(items) + "\n</ol>"

    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <title>{title}</title>
        </head>
        <body>
          <h2>{title}</h2>
          <p>{timestamp}</p>
          {body}
        </body>
        </html>
        """
    ).strip()


def save_preview(config: dict, base_dir: Path, text: str, html_text: str, now: datetime) -> tuple[Path, Path]:
    out_dir = base_dir / config["app"]["output_dir"]
    stamp = now.strftime("%Y%m%d-%H%M%S")
    txt_path = out_dir / f"digest-{stamp}.txt"
    html_path = out_dir / f"digest-{stamp}.html"
    txt_path.write_text(text, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return txt_path, html_path


def send_email(config: dict, text: str, html_text: str, now: datetime) -> None:
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        raise RuntimeError("Email is disabled in config.toml")

    sender = email_config.get("sender", "")
    receiver = email_config.get("receiver", "")
    password = os.environ.get("NEWS_SMTP_PASSWORD", "")
    if not sender or not receiver or not password:
        raise RuntimeError("Missing sender, receiver, or NEWS_SMTP_PASSWORD")

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = f"{config['app'].get('title', '技术文章自动汇总')}-{now.strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    with smtplib.SMTP_SSL(
        email_config.get("smtp_server", "smtp.qq.com"),
        int(email_config.get("smtp_port", 465)),
        timeout=20,
    ) as server:
        server.login(sender, password)
        server.send_message(msg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect technical articles and send a daily digest.")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Generate preview only; do not send email or mark sent")
    parser.add_argument("--ignore-db", action="store_true", help="Ignore dedupe database when selecting articles")
    parser.add_argument("--limit", type=int, default=None, help="Override digest article limit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = base_dir / config_path

    config = load_config(config_path)
    ensure_dirs(config, base_dir)

    timezone = ZoneInfo(config["app"].get("timezone", "Asia/Shanghai"))
    now = datetime.now(timezone)
    limit = args.limit or int(config["app"].get("limit", 10))

    conn = open_db(config, base_dir)
    try:
        all_articles = fetch_hacker_news(config)
        selected = select_articles(
            all_articles,
            config=config,
            conn=conn,
            limit=limit,
            ignore_db=args.ignore_db,
        )

        text = render_text(selected, config, now)
        html_text = render_html(selected, config, now)
        txt_path, html_path = save_preview(config, base_dir, text, html_text, now)

        print(text)
        print(f"Preview written: {txt_path}")
        print(f"HTML preview written: {html_path}")

        if args.dry_run:
            return

        send_email(config, text, html_text, now)
        mark_sent(conn, selected, now)
        print("Email sent and articles marked as sent.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
