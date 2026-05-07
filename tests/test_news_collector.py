"""뉴스 수집기 — parse_feed + collect_recent_news 단위."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from auto_trading._common import KST
from auto_trading.news.collector import (
    NewsItem,
    collect_recent_news,
    parse_feed,
)


def _entry(
    *,
    title: str,
    summary: str = "",
    link: str = "https://example.com/a",
    published: datetime | None = None,
) -> dict:
    """published_parsed 는 time.struct_time 형태로 feedparser 가 채움."""
    pub = published or datetime(2026, 5, 7, 9, 0, tzinfo=KST)
    parsed = time.struct_time(pub.utctimetuple())
    return {
        "title": title,
        "summary": summary,
        "link": link,
        "published_parsed": parsed,
    }


def _feed(*entries: dict) -> dict:
    return {"entries": list(entries)}


def test_parse_feed_extracts_basic_fields() -> None:
    feed = _feed(
        _entry(title="삼성전자 신고가", summary="005930 매출 호조", link="https://h.com/1")
    )
    items = parse_feed(feed, source_label="한국경제")
    assert len(items) == 1
    n = items[0]
    assert n.title == "삼성전자 신고가"
    assert n.url == "https://h.com/1"
    assert n.source == "한국경제"
    assert "005930" in n.tickers


def test_parse_feed_skips_entries_without_title_or_link() -> None:
    feed = _feed(
        _entry(title="ok", link="https://h.com/1"),
        _entry(title="", link="https://h.com/2"),
        _entry(title="no link", link=""),
    )
    items = parse_feed(feed, source_label="한국경제")
    assert len(items) == 1
    assert items[0].title == "ok"


def test_parse_feed_skips_entries_without_published_time() -> None:
    """published_parsed 도 published 문자열도 없으면 skip."""
    feed = {
        "entries": [
            {"title": "no time", "link": "https://h.com/1", "summary": ""},
        ]
    }
    items = parse_feed(feed, source_label="?")
    assert items == []


def test_parse_feed_extracts_multiple_tickers_and_dedupes() -> None:
    feed = _feed(
        _entry(
            title="005930 035420 005930",  # 005930 중복
            summary="000660 also mentioned",
            link="https://h.com/1",
        )
    )
    items = parse_feed(feed, source_label="?")
    assert items[0].tickers == ("000660", "005930", "035420")


def test_parse_feed_truncates_long_summary() -> None:
    long = "a" * 500
    feed = _feed(_entry(title="t", summary=long))
    items = parse_feed(feed, source_label="?")
    assert len(items[0].summary) == 240
    assert items[0].summary.endswith("…")


def test_collect_recent_news_uses_cache(tmp_path: Path) -> None:
    """같은 날 재호출 시 fetcher 호출 X — 캐시에서 로드."""
    fixed_now = datetime(2026, 5, 7, 12, 0, tzinfo=KST)
    feed = _feed(
        _entry(
            title="A",
            link="https://h.com/1",
            published=fixed_now - timedelta(hours=1),
        )
    )
    calls = {"count": 0}

    def _fetcher(_url):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return feed

    out1 = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=fixed_now,
        fetcher=_fetcher,
    )
    assert len(out1) == 1
    assert calls["count"] == 1

    # 같은 날 재호출 — 캐시 히트, fetcher 안 부름.
    out2 = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=fixed_now,
        fetcher=_fetcher,
    )
    assert len(out2) == 1
    assert calls["count"] == 1


def test_collect_recent_news_filters_old_items(tmp_path: Path) -> None:
    """`days=3` 이면 4일 전 뉴스는 제외 — 정렬·필터 검증."""
    now = datetime(2026, 5, 7, 12, 0, tzinfo=KST)
    feed = _feed(
        _entry(title="recent", link="https://h.com/1", published=now - timedelta(days=1)),
        _entry(title="old", link="https://h.com/2", published=now - timedelta(days=5)),
    )
    items = collect_recent_news(
        days=3,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=now,
        fetcher=lambda _url: feed,
    )
    assert [it.title for it in items] == ["recent"]


def test_collect_recent_news_handles_fetch_failure(tmp_path: Path) -> None:
    """fetcher 가 raise 해도 빈 리스트 반환 (다른 feed 시도 진행)."""
    now = datetime(2026, 5, 7, 12, 0, tzinfo=KST)

    def _fail(_url):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    items = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=now,
        fetcher=_fail,
    )
    assert items == []


def test_collect_recent_news_round_trip_via_cache(tmp_path: Path) -> None:
    """cache 저장된 NewsItem 이 그대로 로드된다."""
    now = datetime(2026, 5, 7, 12, 0, tzinfo=KST)
    feed = _feed(
        _entry(
            title="삼성전자",
            summary="005930",
            link="https://h.com/1",
            published=now - timedelta(hours=2),
        )
    )
    out1 = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=now,
        fetcher=lambda _url: feed,
    )
    out2 = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=now,
        fetcher=lambda _url: feed,
    )
    assert out1 == out2
    assert isinstance(out1[0], NewsItem)
    assert "005930" in out1[0].tickers


def test_collect_recent_news_sorts_descending(tmp_path: Path) -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=KST)
    feed = _feed(
        _entry(title="oldest", link="https://h.com/1", published=now - timedelta(days=2)),
        _entry(title="newest", link="https://h.com/2", published=now - timedelta(hours=1)),
        _entry(title="mid", link="https://h.com/3", published=now - timedelta(days=1)),
    )
    items = collect_recent_news(
        days=7,
        feed_urls=("https://h.com",),
        cache_root=tmp_path,
        now=now,
        fetcher=lambda _url: feed,
    )
    titles = [it.title for it in items]
    assert titles == ["newest", "mid", "oldest"]
