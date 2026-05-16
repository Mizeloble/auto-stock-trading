"""뉴스 수집기 — RSS feedparser + JSON 캐시.

24단계 Phase A. 토요일 cron(`scripts/insight-weekly.sh`) 이 직전 7일치 뉴스를
LLM 에 입력하기 위한 정규화 수집층.

설계:
- 외부 의존은 `feedparser` 만 (이미 의존성 추가). httpx 같은 직접 HTTP 호출 X —
  feedparser 가 내부적으로 처리하며 User-Agent 도 설정 가능.
- 기본 RSS URL 은 한국 시장에 가까운 공개 feed (한경·매경) — 네이버가 공식 RSS
  지원을 끊었으므로 대체. 사용자가 `feed_urls` 인자로 다른 URL 주입 가능.
- 캐시: `data/news/YYYY-MM-DD.json`. 같은 날 재호출 시 RSS 호출 회피.
- 종목 코드 추출은 단순 6자리 정규식 — 한국 종목코드 패턴. 이름→코드 매핑은
  안 함(LLM 이 본문 보고 판단).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import feedparser
import structlog

from .._common import KST

log = structlog.get_logger(__name__)

#: 기본 RSS feed URL 목록 — day-trade 분석에 의미 있도록 **시장·증권·경제** 카테고리만.
#: "전체 뉴스(all-news)" feed 는 사회·연예 노이즈가 너무 많아 LLM 입력으로 부적합.
#: 사용자가 settings 또는 collect_recent_news(feed_urls=...) 로 override 가능.
#:
#: 2026-05-10 검증 (feedparser.parse → entries 수):
#:   ✅ 한경 finance/economy (각각 ~50건)
#:   ✅ 연합뉴스 economy (120) / market (120)  — 헤드라인·매크로 다양성
#:   ✅ SBS Biz 경제 섹션 (29) — 방송사 시각
#:   ✅ 파이낸셜뉴스 finance (9) — 금융 전문지
#:   ❌ 매경(file.mk.co.kr) · 이데일리 · 머니투데이 · 서울경제 — 빈 응답/bozo
#: 한 매체 편향 완화 + LLM 이 헤드라인 다양성으로 시장 무드 판단하도록 6개 소스로 확대.
#: 입력 건수는 proposal.py `_summarize_news(max_items=80)` 가 캡.
DEFAULT_FEED_URLS: tuple[str, ...] = (
    "https://www.hankyung.com/feed/finance",  # 한국경제 증권
    "https://www.hankyung.com/feed/economy",  # 한국경제 경제
    "https://www.yna.co.kr/rss/economy.xml",  # 연합뉴스 경제
    "https://www.yna.co.kr/rss/market.xml",  # 연합뉴스 시장
    "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=02",  # SBS Biz 경제
    "https://www.fnnews.com/rss/r20/fn_realnews_finance.xml",  # 파이낸셜뉴스 금융
)

#: 캐시 파일명에 포함되는 schema 버전 — feed URL 이나 NewsItem 구조 변경 시 bump
#: 해서 기존 캐시를 자동 무효화. 같은 날 호출이라도 새 schema 면 새 RSS 호출.
#: v3 (2026-05-10): RSS 소스 4개 추가 + summary truncate 240→400 + force_refresh 누적 병합.
CACHE_SCHEMA_VERSION = "v3"

#: 한국 종목 코드 — 단어 경계 + 6자리 숫자.
_TICKER_RE = re.compile(r"\b(\d{6})\b")

USER_AGENT = "auto-trade-bot/1.0 (+kiwoom paper-day weekly insight)"


@dataclass(frozen=True)
class NewsItem:
    """RSS feed 한 항목 → 정규화 뉴스 도메인 객체."""

    title: str
    summary: str
    url: str
    published_at: datetime  # KST
    source: str  # 사람이 읽는 출처 라벨 (한경 / 매경 / ...)
    tickers: tuple[str, ...] = field(default_factory=tuple)


def parse_feed(raw: bytes | str | dict[str, Any], *, source_label: str) -> list[NewsItem]:
    """feedparser 결과(dict) → NewsItem 리스트. 단위 테스트가 직접 호출.

    무엇/왜:
    - feedparser.parse() 의 결과를 NewsItem 으로 정규화. 외부 호출 없이 단위
      테스트 가능 (fake dict 주입).
    - published_parsed 가 None 이면 그 항목 skip — 시간 정렬·필터에 필요.
    - 종목 코드는 title + summary 에서 6자리 추출 (정확하지 않아도 LLM 이 본문
      읽고 판단).

    어떻게 확인: tests/test_news_collector.py 가 다양한 feed dict 로 검증.
    """
    parsed = raw if isinstance(raw, dict) else feedparser.parse(raw)
    entries = parsed.get("entries") or []
    items: list[NewsItem] = []
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        summary = str(entry.get("summary") or entry.get("description") or "").strip()
        url = str(entry.get("link") or "").strip()
        if not title or not url:
            continue
        published = _extract_published(entry)
        if published is None:
            continue
        text = f"{title}\n{summary}"
        tickers = tuple(sorted(set(_TICKER_RE.findall(text))))
        items.append(
            NewsItem(
                title=title,
                summary=_truncate(summary, 400),
                url=url,
                published_at=published,
                source=source_label,
                tickers=tickers,
            )
        )
    return items


def _extract_published(entry: dict[str, Any]) -> datetime | None:
    """published_parsed → KST datetime. 없으면 published 문자열 fallback."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is not None:
        try:
            year, month, day, hour, minute, second = parsed[:6]
            dt = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
            return dt.astimezone(KST)
        except TypeError, ValueError:
            return None
    raw = entry.get("published") or entry.get("updated")
    if isinstance(raw, str):
        # feedparser 가 일부 포맷을 못 잡는 경우 — RFC 822 기본만 처리.
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(KST)
        except TypeError, ValueError:
            return None
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _source_label_for(url: str) -> str:
    if "hankyung" in url:
        return "한국경제"
    if "yna.co.kr" in url:
        return "연합뉴스"
    if "sbs.co.kr" in url:
        return "SBS Biz"
    if "fnnews.com" in url:
        return "파이낸셜뉴스"
    if "mk.co.kr" in url:
        return "매일경제"
    if "naver" in url:
        return "네이버"
    if "dart" in url:
        return "DART 공시"
    return url


def _cache_path(root: Path, day: datetime) -> Path:
    return root / f"news-{CACHE_SCHEMA_VERSION}-{day.date().isoformat()}.json"


def _save_cache(items: list[NewsItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_to_json(it) for it in items]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cache(path: Path) -> list[NewsItem]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [_from_json(d) for d in raw]


def _to_json(item: NewsItem) -> dict[str, Any]:
    d = asdict(item)
    d["published_at"] = item.published_at.isoformat()
    d["tickers"] = list(item.tickers)
    return d


def _from_json(d: dict[str, Any]) -> NewsItem:
    return NewsItem(
        title=str(d["title"]),
        summary=str(d.get("summary") or ""),
        url=str(d["url"]),
        published_at=datetime.fromisoformat(str(d["published_at"])),
        source=str(d.get("source") or ""),
        tickers=tuple(d.get("tickers") or []),
    )


def collect_recent_news(
    *,
    days: int = 7,
    feed_urls: tuple[str, ...] = DEFAULT_FEED_URLS,
    cache_root: Path | str = "data/news",
    now: datetime | None = None,
    fetcher: Any = None,
    force_refresh: bool = False,
) -> list[NewsItem]:
    """직전 `days` 일치 뉴스 → 캐시 로드/갱신 → published_at 내림차순 list.

    무엇/왜:
    - 매일 호출 가능. 같은 날 재호출 시 캐시(`data/news/news-{ver}-YYYY-MM-DD.json`)
      에서 로드 → RSS 호출 회피 + LLM 호출 시점에 안정적 입력.
    - `force_refresh=True` 면 같은 날 캐시가 있어도 RSS 를 새로 받아 url 키로
      dedupe 후 기존 캐시와 **누적 병합** 저장. 토요일 06:00 prefetch + 09:00
      LLM cron 사이의 새 뉴스를 잃지 않기 위함 (`scripts/news-prefetch.sh`).
    - feed 가 비거나 fetch 실패해도 빈 리스트 반환 — 호출자가 안전 종료.
    - `fetcher` 인자는 단위 테스트에서 feedparser 회피 — None 이면 실제 호출.

    어떻게 확인: tests/test_news_collector.py 의 cache hit/miss + 누적 병합 시나리오.
    """
    today = (now or datetime.now(KST)).astimezone(KST)
    root = Path(cache_root)
    cache = _cache_path(root, today)

    if cache.exists() and not force_refresh:
        log.info("news.cache_hit", path=str(cache))
        cached = _load_cache(cache)
    else:
        existing = _load_cache(cache) if cache.exists() else []
        log.info(
            "news.fetch_start",
            feeds=list(feed_urls),
            force_refresh=force_refresh,
            existing=len(existing),
        )
        fetched: list[NewsItem] = []
        for url in feed_urls:
            label = _source_label_for(url)
            try:
                if fetcher is None:
                    parsed = feedparser.parse(
                        url, agent=USER_AGENT, request_headers={"User-Agent": USER_AGENT}
                    )
                else:
                    parsed = fetcher(url)
            except Exception:
                log.warning("news.fetch_failed", url=url, exc_info=True)
                continue
            try:
                items = parse_feed(parsed, source_label=label)
            except Exception:
                log.warning("news.parse_failed", url=url, exc_info=True)
                continue
            fetched.extend(items)
            if not items:
                # 2026-05-16 관측 — yna economy 가 count:0. feedparser 는 일시적
                # 네트워크 실패도 예외 없이 빈 결과로 삼킨다. bozo 플래그를 함께
                # 남겨 "일시 실패" 와 "정말 빈 피드" 를 사후 구분 가능하게.
                bozo = getattr(parsed, "bozo", 0)
                bozo_exc = getattr(parsed, "bozo_exception", None)
                log.warning(
                    "news.feed_empty",
                    url=url,
                    bozo=bool(bozo),
                    bozo_exception=str(bozo_exc) if bozo_exc else None,
                )
            else:
                log.info("news.fetched", url=url, count=len(items))
        # url 기준 dedupe — 기존 캐시 항목을 먼저 넣고 fetched 가 덮어쓰지 않도록
        # dict 순서를 유지(기존 → 신규). 같은 url 이면 처음 본 것을 유지.
        merged: dict[str, NewsItem] = {}
        for it in existing:
            merged.setdefault(it.url, it)
        for it in fetched:
            merged.setdefault(it.url, it)
        cached = list(merged.values())
        log.info(
            "news.cache_save",
            path=str(cache),
            total=len(cached),
            new=len(cached) - len(existing),
        )
        _save_cache(cached, cache)

    cutoff = today - timedelta(days=days)
    filtered = [it for it in cached if it.published_at >= cutoff]
    filtered.sort(key=lambda it: it.published_at, reverse=True)
    return filtered


__all__ = [
    "DEFAULT_FEED_URLS",
    "USER_AGENT",
    "NewsItem",
    "collect_recent_news",
    "parse_feed",
]
