"""News digest (PydanticAI): fetch a few public RSS feeds, summarize and tag topics.

Falls back to bundled canned articles when the feeds can't be reached.
"""
import json
import random
import re
from typing import Literal

import feedparser
import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .. import config
from ..llm import Run

FRAMEWORK = "pydantic-ai"
FEEDS = {
    "BBC Technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "Hacker News": "https://hnrss.org/frontpage",
    "The Register": "https://www.theregister.com/headlines.atom",
    "NASA": "https://www.nasa.gov/news-release/feed/",
}
ITEMS_PER_FEED = 4


class DigestItem(BaseModel):
    title: str
    source: str
    summary: str = Field(description="one or two sentences")
    topics: list[str] = Field(description="2-4 short lowercase topic tags")
    category: Literal["ai", "security", "business", "science", "hardware", "software", "policy", "other"]


class Digest(BaseModel):
    overview: str = Field(description="three-sentence overview of the day")
    items: list[DigestItem]
    trending_topics: list[str]


agent = Agent(output_type=Digest, retries=2, instructions=(
    "You are an editor producing a morning tech-news digest for an internal newsletter. Summarize each "
    "article neutrally, tag its topics, pick a category, then write a short overview and list trending topics."))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:600]


async def fetch_articles() -> tuple[list[dict], int]:
    picked = random.sample(list(FEEDS.items()), k=random.choice([2, 3]))
    articles, feeds_ok = [], 0
    async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                 headers={"User-Agent": "agent-lab-news-digest/1.0"}) as client:
        for source, url in picked:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            entries = feedparser.parse(resp.content).entries[:ITEMS_PER_FEED]
            if entries:
                feeds_ok += 1
            articles += [{"source": source, "title": _clean(e.get("title")),
                          "summary": _clean(e.get("summary") or e.get("description"))} for e in entries]
    return articles, feeds_ok


async def run(run: Run) -> None:
    articles, feeds_ok = await fetch_articles()
    fallback = not articles
    if fallback:
        articles = random.sample(json.loads((config.DATA_DIR / "news_fallback.json").read_text()), k=6)
    text = "\n\n".join(f"[{i + 1}] {a['source']}: {a['title']}\n{a['summary']}" for i, a in enumerate(articles))

    model = OpenAIChatModel(run.model, provider=OpenAIProvider(openai_client=run.openai_client()))
    result = await agent.run(f"Today's articles:\n\n{text}", model=model,
                             model_settings={"max_tokens": 6144, "temperature": 0.3})
    digest = result.output
    run.extra.update({"feeds_ok": feeds_ok, "fallback_used": fallback, "articles": len(articles),
                      "digest_items": len(digest.items), "trending_topics": digest.trending_topics[:6]})
