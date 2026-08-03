"""Best-effort full-text enrichment for verified official web pages."""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, Optional

import requests

from .runtime import VerificationRuntime


_UA = {"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/2.0)"}


class _ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.article_depth = 0
        self.parts: list[str] = []
        self.json_ld: list[str] = []
        self._json_capture = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_d = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            if tag == "script" and "ld+json" in (attrs_d.get("type") or "").lower():
                self._json_capture = True
                self._json_parts = []
            else:
                self.skip += 1
        if tag in {"article", "main"}:
            self.article_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_capture:
            self.json_ld.append("".join(self._json_parts))
            self._json_capture = False
            self._json_parts = []
        elif tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        if tag in {"article", "main"} and self.article_depth:
            self.article_depth -= 1
        if tag in {"p", "h1", "h2", "li"} and self.article_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._json_capture:
            self._json_parts.append(data)
        elif not self.skip and self.article_depth:
            value = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if value:
                self.parts.append(value)


def enrich_official_item(
    item: Dict[str, Any], runtime: VerificationRuntime
) -> Dict[str, Any]:
    """Fetch an official article body without changing its source identity.

    Failure simply leaves the original title/summary in place; the verification
    engine still fails closed if required facts cannot be grounded.
    """
    document = runtime.documents.from_item(item)
    profile = runtime.sources.get(document.source.profile_id)
    if not profile or not profile.is_official or not document.source.verified:
        return item
    url = item.get("source_url")
    if not url or not str(url).startswith(("http://", "https://")):
        return item
    try:
        collection = runtime.config.collection_config
        response = requests.get(
            url, headers=_UA,
            timeout=float(collection["official_page_timeout_seconds"]),
            allow_redirects=True,
        )
        response.raise_for_status()
        content = response.content[: int(collection["official_page_max_bytes"])]
        final_profile = runtime.sources.profile_for_domain(response.url)
        # The final destination must still be controlled by the same configured
        # official publisher. A Google redirect that cannot be resolved is not
        # silently trusted as page content.
        if not final_profile or final_profile.id != profile.id:
            return item
        parser = _ArticleTextParser()
        parser.feed(content.decode(response.encoding or "utf-8", errors="replace"))
        body, date_published = _json_ld_article(parser.json_ld)
        if not body:
            body = " ".join(parser.parts)
        body = re.sub(r"\s+", " ", body or "").strip()
        if len(body) >= 40:
            item = dict(item)
            item["full_text"] = body[:20_000]
            item["source_url"] = response.url
            item.setdefault("metadata", {})["official_page_enriched"] = True
            item["metadata"]["canonical_url"] = response.url
            if date_published and not item.get("created_at"):
                item["created_at"] = date_published
                item["published_at"] = date_published
        return item
    except Exception as exc:
        item = dict(item)
        item.setdefault("metadata", {})["official_page_enrichment_error"] = str(exc)[:200]
        return item


def _json_ld_article(blocks: Iterable[str]) -> tuple[Optional[str], Optional[str]]:
    bodies: list[str] = []
    dates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"NewsArticle", "Article", "ReportageNewsArticle"}:
                if value.get("articleBody"):
                    bodies.append(str(value["articleBody"]))
                if value.get("datePublished"):
                    dates.append(str(value["datePublished"]))
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)

    for block in blocks:
        try:
            visit(json.loads(block))
        except Exception:
            continue
    body = max(bodies, key=len) if bodies else None
    return body, dates[0] if dates else None
