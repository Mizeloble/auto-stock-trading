"""auto-trade verify-symbol CLI — 29단계 PR34 (#19 후속).

키움 일봉 fetch 는 monkeypatch 로 대체 (외부 호출 0). validate_bars 결과가 표/exit
코드에 정확히 반영되는지 검증.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import structlog
from typer.testing import CliRunner

from auto_trading import cli
from auto_trading._common import KST, Bar

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


def _clean_bars(n: int = 30, *, latest_to_past: bool = True) -> list[Bar]:
    """키움 응답처럼 최신→과거 순 (latest_to_past=True) 또는 정순."""
    base = datetime(2026, 5, 1, tzinfo=KST)
    bars = [
        Bar(
            symbol="005930",
            ts=base + timedelta(days=i),
            open=100.0 + i,
            high=105.0 + i,
            low=95.0 + i,
            close=102.0 + i,
            volume=1_000,
        )
        for i in range(n)
    ]
    return list(reversed(bars)) if latest_to_past else bars


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, bars: list[Bar]) -> None:
    """_fetch_daily_bars 를 직접 monkeypatch — 토큰/REST 호출 우회."""

    def _fake_fetch(symbol: str, **kw: object) -> list[Bar]:
        return bars

    monkeypatch.setattr(cli, "_fetch_daily_bars", _fake_fetch)


def test_verify_symbol_clean_data_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, _clean_bars(30))
    result = runner.invoke(cli.app, ["verify-symbol", "005930"])
    assert result.exit_code == 0, result.output
    assert "이슈 0건" in result.output
    assert "통과" in result.output


def test_verify_symbol_lookback_truncates_to_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """100개 fetch 했어도 --days 10 이면 마지막 10개만 검사."""
    _patch_fetch(monkeypatch, _clean_bars(100))
    result = runner.invoke(cli.app, ["verify-symbol", "005930", "--days", "10"])
    assert result.exit_code == 0
    assert "최근 10일" in result.output


def test_verify_symbol_reports_issues_when_data_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """음수 가격·갭 등 broken 데이터 → issue 표 + 카운트 표시 (exit 0 기본)."""
    # 정순으로 만들어 마지막 bar 의 close 를 x5 로 바꿔 +400% 갭 주입.
    # CLI 가 내부에서 키움 순(최신→과거)으로 받아 다시 정순으로 뒤집으므로
    # _patch_fetch 에는 키움 순으로 주입.
    clean = _clean_bars(10, latest_to_past=False)
    perturbed_last = Bar(
        symbol=clean[-1].symbol,
        ts=clean[-1].ts,
        open=clean[-1].open,
        high=clean[-1].high * 5,
        low=clean[-1].low,
        close=clean[-1].close * 5,
        volume=clean[-1].volume,
    )
    perturbed = [*clean[:-1], perturbed_last]
    _patch_fetch(monkeypatch, list(reversed(perturbed)))

    result = runner.invoke(cli.app, ["verify-symbol", "005930", "--days", "30"])
    assert result.exit_code == 0  # 기본은 exit 0
    assert "abnormal_gap" in result.output
    # "이슈 N건" 메시지 (N >= 1).
    assert "이슈" in result.output


def test_verify_symbol_fail_on_issues_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """--fail-on-issues + 갭 → exit 1."""
    clean = _clean_bars(10, latest_to_past=False)
    perturbed_last = Bar(
        symbol=clean[-1].symbol,
        ts=clean[-1].ts,
        open=clean[-1].open,
        high=clean[-1].high * 5,
        low=clean[-1].low,
        close=clean[-1].close * 5,
        volume=clean[-1].volume,
    )
    perturbed = [*clean[:-1], perturbed_last]
    _patch_fetch(monkeypatch, list(reversed(perturbed)))

    result = runner.invoke(cli.app, ["verify-symbol", "005930", "--fail-on-issues"])
    assert result.exit_code == 1


def test_verify_symbol_gap_threshold_zero_skips_gap_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--gap-threshold 0 → 갭 검사 자체 스킵 (다른 검사는 그대로)."""
    clean = _clean_bars(10, latest_to_past=False)
    perturbed_last = Bar(
        symbol=clean[-1].symbol,
        ts=clean[-1].ts,
        open=clean[-1].open,
        high=clean[-1].high * 5,
        low=clean[-1].low,
        close=clean[-1].close * 5,
        volume=clean[-1].volume,
    )
    perturbed = [*clean[:-1], perturbed_last]
    _patch_fetch(monkeypatch, list(reversed(perturbed)))

    result = runner.invoke(cli.app, ["verify-symbol", "005930", "--gap-threshold", "0"])
    assert result.exit_code == 0
    # 갭은 누락이지만 OHLC 불일치(high<close*5 인 봉) 가 잡힐 수 있음. 갭 검사만 끄는지 확인.
    assert "abnormal_gap" not in result.output
