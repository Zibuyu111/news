#!/usr/bin/env python3
import argparse
import base64
import http.client
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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET
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
    summary: str = ""
    has_score: bool = False
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
    text = f"{article.title} {article.url} {article.summary}".lower()
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

    if article.has_score and article.score < min_score:
        return False

    if not keywords:
        return True

    return keyword_score(article, keywords) > 0


def fetch_all_articles(config: dict) -> list[Article]:
    articles: list[Article] = []

    for label, fetcher in (
        ("Hacker News", fetch_hacker_news),
        ("feeds", fetch_feeds),
    ):
        try:
            articles.extend(fetcher(config))
        except RuntimeError as exc:
            print(f"Skip {label}: {exc}")

    return dedupe_articles(articles)


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
                has_score=True,
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
                has_score=True,
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
                has_score=True,
            )
        )

    return articles


def fetch_feeds(config: dict) -> list[Article]:
    feed_config = config.get("feeds", {})
    if not feed_config.get("enabled", False):
        return []

    timeout = int(feed_config.get("timeout", 12))
    retries = int(feed_config.get("retries", 1))
    per_feed_limit = int(feed_config.get("per_feed_limit", 6))
    articles: list[Article] = []

    for source in feed_config.get("sources", []):
        if not source.get("enabled", True):
            continue

        name = str(source.get("name", "")).strip()
        url = str(source.get("url", "")).strip()
        if not name or not url:
            continue

        try:
            feed_text = get_text(url, config=config, timeout=timeout, accept="application/rss+xml, application/atom+xml, application/xml, text/xml", retries=retries)
            parsed = parse_feed(feed_text, source=name, limit=per_feed_limit)
            articles.extend(parsed)
        except RuntimeError as exc:
            print(f"Skip feed {name}: {exc}")

    return articles


def parse_feed(feed_text: str, source: str, limit: int) -> list[Article]:
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid XML: {exc}") from exc

    entries = find_feed_entries(root)
    articles: list[Article] = []
    for entry in entries[:limit]:
        title = clean_text(first_child_text(entry, "title"))
        url = extract_feed_link(entry)
        if not title or not url:
            continue

        summary = clean_summary(
            first_child_text(entry, "summary", "description", "content", "encoded")
        )
        published_at = parse_feed_timestamp(
            first_child_text(entry, "published", "updated", "pubDate", "date")
        )

        articles.append(
            Article(
                source=source,
                title=title,
                url=url,
                discussion_url=url,
                published_at=published_at,
                summary=summary,
            )
        )

    return articles


def find_feed_entries(root: ET.Element) -> list[ET.Element]:
    root_name = local_name(root.tag)
    if root_name == "feed":
        return [child for child in list(root) if local_name(child.tag) == "entry"]

    if root_name == "rss":
        channel = first_child(root, "channel")
        if channel is not None:
            return [child for child in list(channel) if local_name(child.tag) == "item"]

    if root_name == "rdf":
        return [child for child in list(root) if local_name(child.tag) == "item"]

    return [child for child in root.iter() if local_name(child.tag) in {"entry", "item"}]


def first_child(element: ET.Element, *names: str) -> ET.Element | None:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in wanted:
            return child
    return None


def first_child_text(element: ET.Element, *names: str) -> str:
    child = first_child(element, *names)
    if child is None:
        return ""
    return "".join(child.itertext())


def extract_feed_link(entry: ET.Element) -> str:
    for child in list(entry):
        if local_name(child.tag) != "link":
            continue

        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"alternate", ""}:
            return href

        text = clean_text("".join(child.itertext()))
        if text:
            return text

    return clean_text(first_child_text(entry, "guid", "id"))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def clean_summary(value: str, max_length: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    text = clean_text(text)
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def parse_feed_timestamp(value: str) -> int | None:
    value = clean_text(value)
    if not value:
        return None

    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def dedupe_articles(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    deduped: list[Article] = []

    for article in articles:
        key = article_key(article)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(article)

    return deduped


def article_key(article: Article) -> str:
    url = urllib.parse.urldefrag(article.url).url.strip().rstrip("/")
    if not url:
        return f"{article.source}:{article.title}".lower()
    return url.lower()


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


def get_text(url: str, config: dict, timeout: int = 15, accept: str = "text/html", retries: int | None = None) -> str:
    if retries is None:
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
    except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected) as exc:
        raise RuntimeError(f"request failed: {url}: {exc}") from exc


def select_articles(
    articles: list[Article],
    config: dict,
    conn: sqlite3.Connection | None,
    limit: int,
    ignore_db: bool,
) -> list[Article]:
    candidates = [article for article in articles if passes_min_score(article, config)]

    if conn is not None and not ignore_db:
        candidates = [article for article in candidates if not already_sent(conn, article.url)]

    strong = [article for article in candidates if is_relevant(article, config)]
    weak = [article for article in candidates if article not in strong]

    strong.sort(
        key=lambda article: article_rank(article, config),
        reverse=True,
    )

    max_per_source = int(config["filters"].get("max_per_source", 3))
    priority_sources = config["filters"].get("priority_sources", [])
    selected = take_balanced(strong, limit, max_per_source, priority_sources=priority_sources)
    if len(selected) < limit and config["filters"].get("fill_with_top", True):
        weak.sort(key=lambda article: article_rank(article, config), reverse=True)
        selected.extend(
            Article(
                source=article.source,
                title=article.title,
                url=article.url,
                discussion_url=article.discussion_url,
                score=article.score,
                comments=article.comments,
                published_at=article.published_at,
                summary=article.summary,
                has_score=article.has_score,
                matched=False,
            )
            for article in take_balanced(
                weak,
                limit - len(selected),
                max_per_source,
                selected,
                priority_sources=priority_sources,
            )
        )

    return selected[:limit]


def passes_min_score(article: Article, config: dict) -> bool:
    if not article.has_score:
        return True
    return article.score >= int(config["filters"].get("min_score", 0))


def article_rank(article: Article, config: dict) -> tuple[int, int, int, int]:
    return (
        keyword_score(article, config["filters"].get("keywords", [])),
        article.score,
        article.comments,
        article.published_at or 0,
    )


def take_balanced(
    articles: list[Article],
    limit: int,
    max_per_source: int,
    existing: list[Article] | None = None,
    priority_sources: list[str] | None = None,
) -> list[Article]:
    selected: list[Article] = []
    source_counts: dict[str, int] = {}

    for article in existing or []:
        source_counts[article.source] = source_counts.get(article.source, 0) + 1

    if max_per_source <= 0:
        return articles[:limit]

    priority = priority_sources or []
    article_keys: set[str] = set()

    for source in priority:
        for article in articles:
            if len(selected) >= limit:
                return selected
            if article.source != source:
                continue
            if source_counts.get(article.source, 0) >= max_per_source:
                break
            selected.append(article)
            source_counts[article.source] = source_counts.get(article.source, 0) + 1
            article_keys.add(article_key(article))
            break

    source_order = []
    for article in articles:
        if article.source not in source_order:
            source_order.append(article.source)

    for source in source_order:
        if len(selected) >= limit:
            return selected
        if source in priority or source_counts.get(source, 0) >= max_per_source:
            continue
        for article in articles:
            if article.source != source or article_key(article) in article_keys:
                continue
            selected.append(article)
            source_counts[article.source] = source_counts.get(article.source, 0) + 1
            article_keys.add(article_key(article))
            break

    for article in articles:
        if len(selected) >= limit:
            break
        if article_key(article) in article_keys:
            continue
        if source_counts.get(article.source, 0) >= max_per_source:
            continue
        selected.append(article)
        source_counts[article.source] = source_counts.get(article.source, 0) + 1
        article_keys.add(article_key(article))

    if len(selected) < limit:
        for article in articles:
            if len(selected) >= limit:
                break
            if article_key(article) in article_keys:
                continue
            selected.append(article)
            article_keys.add(article_key(article))

    return selected


def render_text(articles: list[Article], config: dict, now: datetime) -> str:
    lines = [
        f"{config['app'].get('title', 'Tech News Digest')} | {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if not articles:
        lines.append("No new articles matched this run.")
        return "\n".join(lines)

    for index, article in enumerate(articles, start=1):
        lines.extend(
            [
                f"{index}. [{article.source}{'' if article.matched else ' / Top Story'}] {article.title}",
                f"   Link: {article.url}",
                f"   Info: {article_meta_text(article)}",
                "",
            ]
        )
        if article.summary:
            lines.insert(-1, f"   Summary: {article.summary}")

    return "\n".join(lines).rstrip() + "\n"


def render_html(articles: list[Article], config: dict, now: datetime) -> str:
    title = html.escape(config["app"].get("title", "Tech News Digest"))
    timestamp = html.escape(now.strftime("%Y-%m-%d %H:%M"))
    article_count = len(articles)
    if article_count == 1:
        count_text = "1 article"
    elif article_count:
        count_text = f"{article_count} articles"
    else:
        count_text = "No new articles"
    font_family = "'Departure Mono', 'Courier New', 'SFMono-Regular', 'SF Mono', Menlo, Consolas, 'PingFang SC', 'Microsoft YaHei', monospace"
    font_face_css = pixel_font_face_css()

    if not articles:
        body = textwrap.dedent(
            """
            <tr>
              <td style="padding: 36px 0 44px; border-top: 1px solid #e5e5e7;">
                <p style="margin: 0; color: #6e6e73; font-size: 15px; line-height: 1.7;">No new articles matched this run.</p>
              </td>
            </tr>
            """
        ).strip()
    else:
        items = []
        for index, article in enumerate(articles, start=1):
            source = html.escape(article.source)
            source_note = "" if article.matched else " / Top Story"
            title_text = html.escape(article.title)
            url = html.escape(article.url, quote=True)
            meta_text = html.escape(article_meta_text(article))
            divider = "" if index == 1 else "border-top: 1px solid #e5e5e7;"
            summary = (
                textwrap.dedent(
                    f"""
                    <p style="margin: 11px 0 0; color: #6e6e73; font-size: 16px; line-height: 1.55; letter-spacing: .01em;">
                      {html.escape(article.summary)}
                    </p>
                    """
                ).strip()
                if article.summary
                else ""
            )
            items.append(
                textwrap.dedent(
                    f"""
                    <tr>
                      <td style="padding: 0; {divider}">
                        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse: collapse;">
                          <tr>
                            <td style="width: 36px; padding: 22px 0 22px; vertical-align: top; color: #a1a1a6; font-size: 11px; line-height: 1.5; letter-spacing: .06em;">{index}</td>
                            <td style="padding: 22px 0 22px; vertical-align: top;">
                              <p style="margin: 0 0 7px; color: #86868b; font-size: 11px; line-height: 1.55; letter-spacing: .08em; text-transform: uppercase;">{source}{source_note} · {meta_text}</p>
                              <h2 style="margin: 0; color: #1d1d1f; font-size: 22px; line-height: 1.35; font-weight: 400; letter-spacing: .015em;">
                                {title_text}
                              </h2>
                              {summary}
                              <p style="margin: 12px 0 0; color: #2a4fd6; font-size: 12px; line-height: 1.6; letter-spacing: .08em; text-transform: uppercase;">
                                <a href="{url}" style="color: #2a4fd6; text-decoration: none;">Read Article</a>{discussion_link_html(article)}
                              </p>
                            </td>
                          </tr>
                        </table>
                      </td>
                    </tr>
                    """
                ).strip()
            )
        body = "\n".join(items)

    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            {font_face_css}
          </style>
          <title>{title}</title>
        </head>
        <body style="margin: 0; padding: 0; background: #f5f5f7; color: #1d1d1f; font-family: {font_family}; font-feature-settings: normal; -webkit-font-smoothing: none; text-rendering: geometricPrecision;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse: collapse; background: #f5f5f7;">
            <tr>
              <td align="center" style="padding: 40px 16px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse: separate; border-spacing: 0; width: 100%; max-width: 680px; background: #ffffff; border-radius: 8px;">
                  <tr>
                    <td style="padding: 44px 42px 26px;">
                      <h1 style="margin: 0; color: #2a4fd6; font-size: 33px; line-height: 1.12; font-weight: 400; letter-spacing: .08em; text-transform: uppercase;">{title}<span style="display: inline-block; width: .55em; height: .9em; margin-left: .45ch; background: #2a4fd6; vertical-align: -.08em;">&nbsp;</span></h1>
                      <p style="margin: 14px 0 0; color: #6e6e73; font-size: 12px; line-height: 1.7; letter-spacing: .08em; text-transform: uppercase;">{timestamp} · {count_text}</p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 0 42px 18px;">
                      <p style="margin: 0; color: #6e6e73; font-size: 16px; line-height: 1.65; letter-spacing: .04em;">A balanced technical reading list filtered by keywords, score, and source.</p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 0 42px;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse: collapse;">
                        {body}
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding: 22px 42px 38px;">
                      <p style="margin: 0; padding-top: 18px; border-top: 1px solid #e5e5e7; color: #a1a1a6; font-size: 11px; line-height: 1.7; letter-spacing: .08em; text-transform: uppercase;">Generated by my_news.</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """
    ).strip()


def pixel_font_face_css() -> str:
    font_path = Path(__file__).resolve().parent / "assets" / "fonts" / "DepartureMono-Regular.woff2"
    if font_path.exists():
        font_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
        src = f"url('data:font/woff2;base64,{font_data}') format('woff2')"
    else:
        src = "url('../assets/fonts/DepartureMono-Regular.woff2') format('woff2')"

    return textwrap.dedent(
        f"""
        @font-face {{
          font-family: 'Departure Mono';
          src: {src};
          font-weight: 400;
          font-style: normal;
          font-display: swap;
        }}
        """
    ).strip()


def article_meta_text(article: Article) -> str:
    parts = []
    if article.has_score:
        parts.append(f"{article.score} points / {article.comments} comments")
    if article.published_at:
        parts.append(datetime.fromtimestamp(article.published_at, timezone.utc).strftime("%Y-%m-%d"))
    return " / ".join(parts) if parts else "RSS/Atom"


def discussion_link_html(article: Article) -> str:
    if not article.has_score or article.discussion_url == article.url:
        return ""
    return f' <span style="color: #d2d2d7;">·</span> <a href="{html.escape(article.discussion_url, quote=True)}" style="color: #2a4fd6; text-decoration: none;">HN Discussion</a>'


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
    msg["Subject"] = f"{config['app'].get('title', 'Tech News Digest')}-{now.strftime('%Y-%m-%d')}"
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
        all_articles = fetch_all_articles(config)
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
