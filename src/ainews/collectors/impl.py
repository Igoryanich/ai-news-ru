"""Concrete collectors: RSS/Atom (incl. GitHub releases, YouTube, Reddit, arXiv, Habr),
Hacker News (Algolia API), Hugging Face daily papers, HTML listing pages, Google Trends."""

import calendar
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..textutil import canonical_url, html_to_text
from .base import CollectResult, FetchError, RawItem, Robots, SourceSpec, fetch

_PRERELEASE = re.compile(r"(?<![a-z])(rc|alpha|beta|dev|nightly|preview|pre)\d*\b", re.I)


def _entry_time(entry) -> datetime | None:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return None
    return datetime.fromtimestamp(calendar.timegm(stamp), UTC)


def _is_minor_or_major_release(title: str) -> bool:
    """For GitHub releases: keep x.y / x.y.0; drop patches, RCs, nightlies and non-version
    tags (some repos' releases.atom also lists branch tags such as `trunk/<sha>`)."""
    if _PRERELEASE.search(title) or "/" in title.split(":")[0]:
        return False
    m = re.search(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?\b", title)
    if not m:
        return False
    return not m.group(3) or int(m.group(3)) == 0


class FeedCollector:
    async def collect(self, client: httpx.AsyncClient, spec: SourceSpec, is_known) -> CollectResult:
        body, status = await fetch(client, spec.url)
        feed = feedparser.parse(body)
        if feed.bozo and not feed.entries:
            raise FetchError("invalid feed", status)
        opts = spec.options
        items: list[RawItem] = []
        for entry in feed.entries[: int(opts.get("limit", 40))]:
            link = entry.get("link")
            when = _entry_time(entry)
            if not link or not when:
                continue  # undated items are never treated as fresh news
            title = html_to_text(entry.get("title", ""), 400)
            if opts.get("release_filter") == "minor" and not _is_minor_or_major_release(title):
                continue
            content = ""
            if entry.get("content"):
                content = entry["content"][0].get("value", "")
            summary = html_to_text(content or entry.get("summary", ""), 6000)
            if spec.kind == "youtube":
                summary = html_to_text(entry.get("media_description") or entry.get("summary", ""), 3000)
            items.append(RawItem(url=link, title=title, summary=summary, published_at=when))
        return CollectResult(items, status)


class HackerNewsCollector:
    """Algolia HN API. `social` = points, `comments` = num_comments."""

    async def collect(self, client, spec, is_known) -> CollectResult:
        body, status = await fetch(client, spec.url)
        data = json.loads(body)
        min_points = int(spec.options.get("min_points", 0))
        items = []
        for hit in data.get("hits", []):
            points = int(hit.get("points") or 0)
            if points < min_points or not hit.get("title"):
                continue
            hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            items.append(
                RawItem(
                    url=hit.get("url") or hn_url,
                    title=html_to_text(hit["title"], 400),
                    summary=html_to_text(hit.get("story_text") or "", 4000),
                    published_at=datetime.fromtimestamp(int(hit["created_at_i"]), UTC),
                    social=points,
                    comments=int(hit.get("num_comments") or 0),
                    extra={"discussion": hn_url},
                )
            )
        return CollectResult(items, status)


class HFPapersCollector:
    """Hugging Face daily papers: community-curated arXiv papers with upvotes."""

    async def collect(self, client, spec, is_known) -> CollectResult:
        body, status = await fetch(client, spec.url)
        min_upvotes = int(spec.options.get("min_upvotes", 0))
        items = []
        for row in json.loads(body):
            paper = row.get("paper") or {}
            upvotes = int(paper.get("upvotes") or 0)
            pid = paper.get("id")
            stamp = row.get("publishedAt") or paper.get("publishedAt")
            if not pid or upvotes < min_upvotes or not stamp:
                continue
            items.append(
                RawItem(
                    url=f"https://arxiv.org/abs/{pid}",
                    title=html_to_text(paper.get("title") or row.get("title", ""), 400),
                    summary=html_to_text(paper.get("summary", ""), 4000),
                    published_at=datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC),
                    social=upvotes,
                    comments=int(row.get("numComments") or 0),
                    extra={"discussion": f"https://huggingface.co/papers/{pid}"},
                )
            )
        return CollectResult(items, status)


_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")


def parse_loose_date(value: str | None, now: datetime | None = None) -> datetime | None:
    """ISO timestamps or date-only strings. Date-only → noon UTC, clamped to `now`."""
    if not value:
        return None
    value = value.strip()
    now = now or datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        if len(value) <= 10:
            dt = datetime.combine(dt.date(), time(12), UTC)
        return min(dt.astimezone(UTC), now)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(value, fmt).date()
            return min(datetime.combine(d, time(12), UTC), now)
        except ValueError:
            continue
    return None


def _meta_date(soup: BeautifulSoup) -> datetime | None:
    for attr in ("article:published_time", "og:published_time", "datePublished", "pubdate", "date"):
        tag = soup.find("meta", attrs={"property": attr}) or soup.find("meta", attrs={"name": attr})
        if tag and (d := parse_loose_date(tag.get("content"))):
            return d
    for script in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
        if m and (d := parse_loose_date(m.group(1))):
            return d
    t = soup.find("time")
    if t:
        return parse_loose_date(t.get("datetime") or t.get_text(strip=True))
    return None


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


class WebListingCollector:
    """Sources without a feed: parse the listing page, open only NEW article links.

    Options: link_pattern (regex on absolute URL, required), max_new (per run, default 6).
    Date: listing <time>, else article meta/JSON-LD; items without a date are skipped.
    Only pages allowed by robots.txt are fetched.
    """

    async def collect(self, client, spec, is_known: Callable[[str], bool]) -> CollectResult:
        robots = Robots(client, client.headers.get("User-Agent", "*"))
        if not await robots.allowed(spec.url):
            raise FetchError("disallowed by robots.txt")
        body, status = await fetch(client, spec.url)
        soup = BeautifulSoup(body, "html.parser")
        pattern = re.compile(spec.options["link_pattern"])
        seen: dict[str, tuple[str, datetime | None]] = {}
        for a in soup.find_all("a", href=True):
            url = urljoin(spec.url, a["href"]).split("#")[0].split("?")[0]
            if not pattern.match(url) or url in seen:
                continue
            t = a.find("time") or (a.parent.find("time") if a.parent else None)
            when = parse_loose_date(t.get("datetime") or t.get_text(strip=True)) if t else None
            seen[url] = (a.get_text(" ", strip=True), when)
        items, notes, opened = [], [], 0
        max_new = int(spec.options.get("max_new", 6))
        for url, (anchor_text, when) in seen.items():
            try:
                if is_known(canonical_url(url)):
                    continue
            except ValueError:
                continue
            if opened >= max_new:
                notes.append("max_new reached; remaining links next run")
                break
            if not await robots.allowed(url):
                continue
            opened += 1
            try:
                page, _ = await fetch(client, url, retries=0)
            except FetchError:
                continue
            ps = BeautifulSoup(page, "html.parser")
            title = _meta(ps, "og:title", "twitter:title") or (ps.title.get_text(strip=True) if ps.title else "")
            desc = _meta(ps, "og:description", "description", "twitter:description")
            when = when or _meta_date(ps)
            if not when:
                notes.append("undated link skipped")
                continue
            items.append(RawItem(url=url, title=title or anchor_text[:300], summary=desc, published_at=when))
        return CollectResult(items, status, notes=notes)


class TrendsCollector:
    """Google Trends "Trending now" RSS → a list of trending search terms (a ranking signal only)."""

    async def collect(self, client, spec, is_known) -> CollectResult:
        body, status = await fetch(client, spec.url)
        feed = feedparser.parse(body)
        terms = [html_to_text(e.get("title", ""), 120) for e in feed.entries if e.get("title")]
        return CollectResult([], status, signals={"trending_terms": terms})


async def fetch_article_text(client: httpx.AsyncClient, url: str, limit: int = 6000) -> str:
    """Best-effort main-text extraction for richer drafting. Respects robots.txt."""
    robots = Robots(client, client.headers.get("User-Agent", "*"))
    try:
        if not await robots.allowed(url):
            return ""
        body, _ = await fetch(client, url, retries=0)
    except FetchError:
        return ""
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "h2", "h3"])]
    text = "\n".join(p for p in paragraphs if len(p) > 40)
    return text[:limit]


COLLECTORS = {
    "rss": FeedCollector(),
    "youtube": FeedCollector(),
    "github": FeedCollector(),
    "reddit": FeedCollector(),
    "arxiv": FeedCollector(),
    "hn": HackerNewsCollector(),
    "hf_papers": HFPapersCollector(),
    "web": WebListingCollector(),
    "trends": TrendsCollector(),
}


def freshness_cutoff(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)
