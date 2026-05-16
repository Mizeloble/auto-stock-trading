"""프로파일 YAML 로딩을 초보 친화 한국어 오류로 감싸는 공유 헬퍼.

왜 별도 모듈인가:
- `cli/__init__` 은 `daytrade.cli_helpers` 를 import 하므로, 헬퍼를 `__init__` 에 두면
  cli_helpers → cli 역참조 시 순환 import 위험. 이 모듈은 `auto_trading.cli` 네임스페이스
  에 의존하지 않는 독립 모듈이라 어디서 import 해도 안전하다.
- raw `FileNotFoundError`/`yaml.YAMLError`/pydantic `ValidationError` 트레이스백 대신
  "어떤 파일의 어떤 키가 왜 잘못됐는지" 를 한국어 한 줄로 보여 주고 `typer.Exit(1)`.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from ..daytrade.profile import DaytradeProfile
from ..strategy.profile import StrategyProfile

console = Console()


def _exit_with_validation(path: Path, exc: ValidationError) -> None:
    console.print(f"[red]프로파일 검증 실패 ({path}):[/red]")
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(최상위)"
        console.print(f"  [yellow]{loc}[/yellow]: {err.get('msg', '?')}")
    console.print("[dim]YAML 의 해당 키 값을 허용 범위 안으로 고치세요.[/dim]")
    raise typer.Exit(code=1)


def _exit_friendly(path: Path, exc: Exception) -> None:
    if isinstance(exc, FileNotFoundError):
        console.print(f"[red]프로파일 파일을 찾을 수 없습니다:[/red] {path}")
        console.print("[yellow]경로를 확인하세요. 예: strategies/daytrade.yaml[/yellow]")
    elif isinstance(exc, yaml.YAMLError):
        console.print(f"[red]YAML 문법 오류 ({path}):[/red] {exc}")
        console.print("[yellow]들여쓰기·콜론·따옴표를 확인하세요.[/yellow]")
    elif isinstance(exc, ValueError):
        # load_yaml 이 "최상위는 매핑이어야 합니다" 등 이미 한국어로 던지는 경우.
        console.print(f"[red]프로파일 오류 ({path}):[/red] {exc}")
    else:  # pragma: no cover - 예상 외 예외는 그대로 노출이 안전
        raise exc
    raise typer.Exit(code=1)


def load_strategy_profile_or_exit(path: Path) -> StrategyProfile:
    try:
        return StrategyProfile.load_yaml(path)
    except ValidationError as exc:
        _exit_with_validation(path, exc)
        raise  # pragma: no cover - _exit_* 가 항상 Exit
    except (FileNotFoundError, yaml.YAMLError, ValueError) as exc:
        _exit_friendly(path, exc)
        raise  # pragma: no cover


def load_daytrade_profile_or_exit(path: Path) -> DaytradeProfile:
    try:
        return DaytradeProfile.load_yaml(path)
    except ValidationError as exc:
        _exit_with_validation(path, exc)
        raise  # pragma: no cover
    except (FileNotFoundError, yaml.YAMLError, ValueError) as exc:
        _exit_friendly(path, exc)
        raise  # pragma: no cover


def validate_strategy_source(
    *,
    profile: Path | None,
    strategy: str | None,
    symbol: str | None,
) -> None:
    """`--profile` vs `--strategy`+`--symbol(s)` 상호배타 검증 (공유).

    backtest·paper 양쪽에 중복돼 메시지가 갈리던 것을 한 곳으로 통합.
    """
    if profile is not None and (strategy or symbol):
        raise typer.BadParameter(
            "--profile 와 --strategy/--symbol 을 동시에 줄 수 없습니다. 둘 중 하나만 선택하세요."
        )
    if profile is None and not (strategy and symbol):
        raise typer.BadParameter(
            "--profile 단독, 또는 (--strategy + --symbol) 함께 — 둘 중 하나가 필요합니다."
        )


__all__ = [
    "load_daytrade_profile_or_exit",
    "load_strategy_profile_or_exit",
    "validate_strategy_source",
]
