"""백테스트 명령: backtest (walk-forward · CPCV 포함) / backtest-compare."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import typer
from rich.table import Table

from auto_trading import cli

from ..backtest.engine import Backtester, save_result
from ..strategy import registry


@cli.app.command()
def backtest(
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="전략 이름 (예: sma_crossover). --profile 과 둘 중 하나."
    ),
    symbol: str | None = typer.Option(None, "--symbol", help="종목코드. --profile 사용 시 생략."),
    start: str = typer.Option(..., "--from", "-f", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--to", help="YYYY-MM-DD, 생략 시 오늘"),
    cash: float = typer.Option(10_000_000.0, "--cash", help="초기 예수금"),
    out_dir: Path | None = typer.Option(None, "--out", help="결과 저장 폴더"),
    profile: Path | None = typer.Option(
        None, "--profile", help="전략 프로파일 YAML 경로. --strategy/--symbol 대체."
    ),
    walk_forward_spec: str | None = typer.Option(
        None,
        "--walk-forward",
        help="walk-forward 모드. 'window/step' 일 단위 (예: '90/30' = 90일 윈도우, 30일 간격).",
    ),
    cpcv_spec: str | None = typer.Option(
        None,
        "--cpcv",
        help=(
            "Purged K-Fold CV 모드. 'N' 또는 'N/E' (예: '10' = 10 fold, '10/2' = "
            "10 fold + 양 끝 2 봉 embargo). 29단계 PR37 (#2 Phase 1)."
        ),
    ),
) -> None:
    """과거 일봉으로 전략 시뮬레이션.

    \\--walk-forward 활성 시 단일 백테스트 대신 슬라이딩 윈도우로 분할 평가 —
    윈도우별 metric 분포로 전략의 시간 일관성 확인.

    \\--cpcv 활성 시 균등 N-fold 분할 + embargo purge → fold 별 Sharpe/PSR 분포 +
    p05/p95 신뢰구간. walk-forward 와는 상호 배타.
    """
    cli.validate_strategy_source(profile=profile, strategy=strategy, symbol=symbol)
    if walk_forward_spec is not None and cpcv_spec is not None:
        raise typer.BadParameter("--walk-forward 와 --cpcv 는 동시에 줄 수 없습니다.")

    strategy_name: str
    strategy_params: dict[str, object]
    if profile is not None:
        prof = cli.load_strategy_profile_or_exit(profile)
        strategy_name = prof.strategy
        symbol = prof.symbol
        strategy_params = dict(prof.params)
        strat = registry.get(prof.strategy, **prof.params)
        cli.console.print(
            f"[cyan]프로파일 로드:[/cyan] {profile.name} "
            f"(strategy={prof.strategy}, params={prof.params})"
        )
    else:
        assert strategy is not None and symbol is not None  # 위 검증 통과 후 보장
        strategy_name = strategy
        strategy_params = {}
        strat = registry.get(strategy)

    base = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start)
    bars = cli._fetch_daily_bars(symbol, base_dt=base, max_pages=10)
    bars = [b for b in bars if b.ts.date() >= start_date]
    if not bars:
        cli.console.print(f"[red]요청한 기간({start}~)에 해당하는 봉이 없습니다.[/red]")
        raise typer.Exit(code=1)

    df = cli.bars_to_dataframe(bars)
    cli.console.print(
        f"[dim]Bars:[/dim] {len(df)} 개 ({df.index[0].date()} → {df.index[-1].date()})"
    )

    # 29단계 PR24 — walk-forward 분기
    if walk_forward_spec is not None:
        window_days, step_days = cli._parse_walk_forward_spec(walk_forward_spec)
        _run_walk_forward(
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            symbol=symbol,
            df=df,
            window_days=window_days,
            step_days=step_days,
            cash=cash,
            out_dir=out_dir,
        )
        return

    # 29단계 PR37/PR44 — CPCV (purged k-fold / combinatorial) 분기
    if cpcv_spec is not None:
        n_folds, embargo_bars, n_test_folds = cli._parse_cpcv_spec(cpcv_spec)
        _run_cpcv(
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            symbol=symbol,
            df=df,
            n_folds=n_folds,
            embargo_bars=embargo_bars,
            n_test_folds=n_test_folds,
            cash=cash,
            out_dir=out_dir,
        )
        return

    bt = Backtester(strategy=strat, initial_cash=cash)
    result = bt.run(symbol, df)

    rep = result.report.as_dict()
    table = Table(title=f"백테스트 결과 · {strategy_name} · {symbol}")
    table.add_column("지표")
    table.add_column("값", justify="right")
    for k, v in rep.items():
        table.add_row(k, str(v))
    cli.console.print(table)

    out = (
        out_dir
        or Path("data/backtests") / f"{strategy_name}_{symbol}_{datetime.now():%Y%m%d_%H%M%S}"
    )
    saved = save_result(result, out)
    cli.console.print(f"[green]결과 저장:[/green] {saved}")


def _run_walk_forward(
    *,
    strategy_name: str,
    strategy_params: dict[str, object],
    symbol: str,
    df,  # pd.DataFrame
    window_days: int,
    step_days: int,
    cash: float,
    out_dir: Path | None,
) -> None:
    """walk-forward 백테스트 실행 + 표 출력 + 결과 저장.

    각 윈도우마다 새 strategy 인스턴스 (state 누설 방지). 저장 형식:
    - windows.csv: 윈도우별 메트릭 행.
    - summary.json: WalkForwardSummary as_dict.
    """
    import csv as _csv
    import json as _json

    from ..backtest.walk_forward import walk_forward

    def _factory():
        return registry.get(strategy_name, **strategy_params)

    result = walk_forward(
        _factory,
        symbol,
        df,
        window_days=window_days,
        step_days=step_days,
        initial_cash=cash,
    )

    if result.summary.n_windows == 0:
        cli.console.print(
            f"[yellow]Walk-forward 윈도우 0건 — 데이터 부족 (window_days={window_days}).[/yellow]"
        )
        raise typer.Exit(code=1)

    # 윈도우 표.
    table = Table(
        title=f"Walk-Forward · {strategy_name} · {symbol} "
        f"(window={window_days}d / step={step_days}d)"
    )
    table.add_column("#", justify="right")
    table.add_column("기간")
    table.add_column("Sharpe", justify="right")
    table.add_column("CAGR%", justify="right")
    table.add_column("MDD%", justify="right")
    table.add_column("WinRate%", justify="right")
    table.add_column("거래", justify="right")
    for i, w in enumerate(result.windows, start=1):
        rep = w.report
        s_color = "green" if rep.sharpe > 0 else "red"
        table.add_row(
            str(i),
            f"{w.start.date()} → {w.end.date()}",
            f"[{s_color}]{rep.sharpe:.3f}[/{s_color}]",
            f"{rep.cagr * 100:+.2f}",
            f"{rep.max_drawdown * 100:.2f}",
            f"{rep.win_rate * 100:.1f}",
            str(rep.num_trades),
        )
    cli.console.print(table)

    # Summary.
    s = result.summary
    summary_table = Table(title="Summary")
    summary_table.add_column("지표")
    summary_table.add_column("값", justify="right")
    summary_table.add_row("윈도우 개수", str(s.n_windows))
    summary_table.add_row("Sharpe 평균", f"{s.sharpe_mean:.3f}")
    summary_table.add_row("Sharpe 표준편차", f"{s.sharpe_std:.3f}")
    summary_table.add_row("Sharpe min/max", f"{s.sharpe_min:.3f} / {s.sharpe_max:.3f}")
    summary_table.add_row("양수 Sharpe 비율", f"{s.positive_sharpe_ratio:.1%}")
    summary_table.add_row("CAGR 평균%", f"{s.cagr_mean_pct:+.2f}")
    summary_table.add_row("CAGR 표준편차%", f"{s.cagr_std_pct:.2f}")
    summary_table.add_row("최악 MDD%", f"{s.mdd_worst_pct:.2f}")
    summary_table.add_row("승률 평균%", f"{s.win_rate_mean_pct:.2f}")
    cli.console.print(summary_table)

    # 저장.
    out = (
        out_dir
        or Path("data/backtests") / f"{strategy_name}_{symbol}_wf_{datetime.now():%Y%m%d_%H%M%S}"
    )
    out.mkdir(parents=True, exist_ok=True)

    windows_path = out / "windows.csv"
    with windows_path.open("w", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(
            ["start", "end", "sharpe", "cagr", "max_drawdown", "win_rate", "num_trades"]
        )
        for w in result.windows:
            r = w.report
            writer.writerow(
                [
                    w.start.isoformat(),
                    w.end.isoformat(),
                    f"{r.sharpe:.6f}",
                    f"{r.cagr:.6f}",
                    f"{r.max_drawdown:.6f}",
                    f"{r.win_rate:.6f}",
                    r.num_trades,
                ]
            )

    summary_path = out / "summary.json"
    summary_path.write_text(
        _json.dumps(
            {
                "n_windows": s.n_windows,
                "sharpe_mean": s.sharpe_mean,
                "sharpe_std": s.sharpe_std,
                "sharpe_min": s.sharpe_min,
                "sharpe_max": s.sharpe_max,
                "cagr_mean_pct": s.cagr_mean_pct,
                "cagr_std_pct": s.cagr_std_pct,
                "mdd_worst_pct": s.mdd_worst_pct,
                "win_rate_mean_pct": s.win_rate_mean_pct,
                "positive_sharpe_ratio": s.positive_sharpe_ratio,
                "window_days": window_days,
                "step_days": step_days,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cli.console.print(f"[green]결과 저장:[/green] {out}")


def _run_cpcv(
    *,
    strategy_name: str,
    strategy_params: dict[str, object],
    symbol: str,
    df,  # pd.DataFrame
    n_folds: int,
    embargo_bars: int,
    n_test_folds: int,
    cash: float,
    out_dir: Path | None,
) -> None:
    """CPCV 실행 + 표 + 저장 — 29단계 PR37 (Phase 1) / PR44 (combinatorial).

    n_test_folds == 1 → purged_kfold (단순 N-fold).
    n_test_folds  > 1 → combinatorial_purged_kfold (C(N,k) 조합 — 더 큰 분포 표본).

    저장:
    - folds.csv: fold/조합 별 메트릭 행.
    - summary.json: CPCVSummary + 요청 파라미터.
    """
    import csv as _csv
    import json as _json
    from dataclasses import asdict as _asdict

    from ..backtest.cpcv import combinatorial_purged_kfold, purged_kfold

    def _factory():
        return registry.get(strategy_name, **strategy_params)

    combinatorial = n_test_folds > 1
    if combinatorial:
        combo_result = combinatorial_purged_kfold(
            _factory,
            symbol,
            df,
            n_folds=n_folds,
            n_test_folds=n_test_folds,
            embargo_bars=embargo_bars,
            initial_cash=cash,
        )
        summary = combo_result.summary
        # (label, n_bars, report) 로 정규화.
        rows = [
            ("+".join(str(i) for i in f.combination), f.n_bars, f.report)
            for f in combo_result.folds
        ]
        sample_count = combo_result.n_combinations
        title_extra = f"n_folds={n_folds} / k={n_test_folds} / C(N,k)={sample_count}"
    else:
        kfold_result = purged_kfold(
            _factory,
            symbol,
            df,
            n_folds=n_folds,
            embargo_bars=embargo_bars,
            initial_cash=cash,
        )
        summary = kfold_result.summary
        rows = [(str(f.index), f.n_bars, f.report) for f in kfold_result.folds]
        sample_count = summary.n_folds
        title_extra = f"n_folds={n_folds} / embargo={embargo_bars}"

    if sample_count == 0:
        cli.console.print(
            f"[yellow]CPCV 표본 0건 — 데이터 부족 또는 분할 실패 (n_folds={n_folds}).[/yellow]"
        )
        raise typer.Exit(code=1)

    # Fold/조합 표 — combinatorial 은 행이 많아 상위 20개만 콘솔에 (CSV 는 전체).
    label_col = "조합" if combinatorial else "#"
    table = Table(title=f"CPCV · {strategy_name} · {symbol} ({title_extra})")
    table.add_column(label_col, justify="right")
    table.add_column("봉", justify="right")
    table.add_column("Sharpe", justify="right")
    table.add_column("PSR", justify="right")
    table.add_column("MDD%", justify="right")
    table.add_column("WinRate%", justify="right")
    table.add_column("거래", justify="right")
    display_rows = rows[:20] if combinatorial else rows
    for label, n_bars, rep in display_rows:
        s_color = "green" if rep.sharpe > 0 else "red"
        table.add_row(
            label,
            str(n_bars),
            f"[{s_color}]{rep.sharpe:.3f}[/{s_color}]",
            f"{rep.probabilistic_sharpe:.3f}",
            f"{rep.max_drawdown * 100:.2f}",
            f"{rep.win_rate * 100:.1f}",
            str(rep.num_trades),
        )
    cli.console.print(table)
    if combinatorial and len(rows) > 20:
        cli.console.print(f"[dim]… 콘솔은 상위 20개만. 전체 {len(rows)}개는 folds.csv 참고.[/dim]")

    # Summary (Phase 1/2 공통 CPCVSummary).
    s = summary
    summary_table = Table(title="CPCV Summary (분포)")
    summary_table.add_column("지표")
    summary_table.add_column("값", justify="right")
    summary_table.add_row("표본 개수", str(s.n_folds))
    summary_table.add_row("Sharpe 평균", f"{s.sharpe_mean:.3f}")
    summary_table.add_row("Sharpe 표준편차", f"{s.sharpe_std:.3f}")
    summary_table.add_row(
        "Sharpe p05 / p50 / p95", f"{s.sharpe_p05:.3f} / {s.sharpe_p50:.3f} / {s.sharpe_p95:.3f}"
    )
    summary_table.add_row("양수 Sharpe 비율", f"{s.positive_sharpe_ratio:.1%}")
    summary_table.add_row("PSR 평균", f"{s.psr_mean:.3f}")
    summary_table.add_row("최악 MDD%", f"{s.mdd_worst_pct:.2f}")
    summary_table.add_row("승률 평균%", f"{s.win_rate_mean_pct:.2f}")
    cli.console.print(summary_table)

    # 저장.
    out = (
        out_dir
        or Path("data/backtests") / f"{strategy_name}_{symbol}_cpcv_{datetime.now():%Y%m%d_%H%M%S}"
    )
    out.mkdir(parents=True, exist_ok=True)

    folds_path = out / "folds.csv"
    with folds_path.open("w", newline="") as csv_file:
        writer = _csv.writer(csv_file)
        writer.writerow(
            [
                "fold" if combinatorial else "index",
                "n_bars",
                "sharpe",
                "probabilistic_sharpe",
                "max_drawdown_pct",
                "win_rate_pct",
                "num_trades",
            ]
        )
        for label, n_bars, rep in rows:
            writer.writerow(
                [
                    label,
                    n_bars,
                    rep.sharpe,
                    rep.probabilistic_sharpe,
                    rep.max_drawdown * 100.0,
                    rep.win_rate * 100.0,
                    rep.num_trades,
                ]
            )

    summary_path = out / "summary.json"
    summary_path.write_text(
        _json.dumps(
            {
                **_asdict(summary),
                "n_folds_requested": n_folds,
                "embargo_bars": embargo_bars,
                "n_test_folds": n_test_folds,
                "combinatorial": combinatorial,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cli.console.print(f"[green]결과 저장:[/green] {out}")


@cli.app.command("backtest-compare")
def backtest_compare(
    run1: Path = typer.Argument(..., help="기준 백테스트 결과 폴더 (report.json 포함)."),
    run2: Path = typer.Argument(..., help="비교 대상 폴더 (보통 새로 돌린 결과)."),
    fail_on_regression: bool = typer.Option(
        False,
        "--fail-on-regression",
        help="회귀 감지 시 exit 1 — CI 가드용. 기본은 표시만(exit 0).",
    ),
) -> None:
    """두 백테스트 결과 비교 — Sharpe/PSR/CAGR/MDD/win-rate 차이 표.

    무엇/왜:
    - 전략·파라미터 변경 후 회귀 즉시 감지 (이 PR 후 Sharpe 0.1 떨어졌나? PSR 이 0.9→0.6?).
    - 각 폴더의 `report.json` (engine.save_result 가 저장) 만 읽음. 외부 호출 X.
    - `--fail-on-regression` 으로 CI 가드 가능 (회귀 발견 시 exit 1).

    어떻게 확인:
    - `auto-trade backtest --strategy sma_crossover --symbol 005930 --from 2024-01-01 --out data/backtests/baseline`
    - 코드 변경 후: `--out data/backtests/new`
    - `auto-trade backtest-compare data/backtests/baseline data/backtests/new`
    """
    import json as _json

    def _load(path: Path) -> dict:
        report = path / "report.json"
        if not report.exists():
            cli.console.print(f"[red]report.json 없음:[/red] {report}")
            raise typer.Exit(code=1)
        try:
            return _json.loads(report.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            cli.console.print(f"[red]파싱 실패:[/red] {report} — {exc}")
            raise typer.Exit(code=1) from exc

    d1 = _load(run1)
    d2 = _load(run2)

    # 메트릭별 (label, key, higher_is_better, format).
    # higher_is_better=None 이면 색상 X (단순 사실).
    # 29단계 PR31 후속 — Probabilistic Sharpe (PSR) 비교 행 추가.
    metrics: list[tuple[str, str, bool | None, str]] = [
        ("초기 자본", "initial_equity", None, "{:,.0f}"),
        ("최종 자본", "final_equity", None, "{:,.0f}"),
        ("총 수익률 %", "total_return_pct", True, "{:+.2f}"),
        ("CAGR %", "cagr_pct", True, "{:+.2f}"),
        ("Sharpe", "sharpe", True, "{:.3f}"),
        ("Probabilistic Sharpe", "probabilistic_sharpe", True, "{:.3f}"),
        ("연환산 변동성 %", "annualized_vol_pct", False, "{:.2f}"),
        ("최대 낙폭 %", "max_drawdown_pct", True, "{:.2f}"),  # 음수라 큰 값(0에 가까움)이 좋음
        ("승률 %", "win_rate_pct", True, "{:.2f}"),
        ("거래 횟수", "num_trades", None, "{:,}"),
        ("라운드트립", "num_round_trips", None, "{:,}"),
    ]

    table = Table(title=f"백테스트 비교 · {run1.name} → {run2.name}")
    table.add_column("항목")
    table.add_column(run1.name, justify="right")
    table.add_column(run2.name, justify="right")
    table.add_column("차이", justify="right")

    regressions: list[str] = []
    for label, key, higher_better, fmt in metrics:
        v1 = d1.get(key)
        v2 = d2.get(key)
        if v1 is None or v2 is None:
            continue
        diff = float(v2) - float(v1)
        # 부호 명시 — 음수면 fmt 가 자동으로 '-' 붙임, 양수는 '+' 수동 prefix.
        diff_str = ("+" if diff > 0 else "") + fmt.format(diff)
        is_regression = False
        if higher_better is None or abs(diff) < 1e-9:
            color = "white"
        elif (diff > 0) == higher_better:
            color = "green"
        else:
            color = "red"
            is_regression = True
        if is_regression:
            regressions.append(label)
        table.add_row(
            label,
            fmt.format(float(v1)),
            fmt.format(float(v2)),
            f"[{color}]{diff_str}[/{color}]",
        )

    cli.console.print(table)

    if regressions:
        joined = ", ".join(regressions)
        cli.console.print(f"[red]회귀 {len(regressions)}건:[/red] {joined}")
        if fail_on_regression:
            raise typer.Exit(code=1)
    else:
        cli.console.print("[green]✓ 회귀 0건[/green]")
