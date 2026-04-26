"""전략 프로파일 — YAML 로 저장·편집 가능한 전략 설정.

사용자는 `auto-trade strategy suggest` 로 만들어진 YAML 의 숫자만 손보면
`backtest --profile` / `paper --profile` 로 같은 전략을 재현 실행할 수 있다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# 새 전략을 추가하면 이 튜플과 Literal 동기화. AdvisorChange 도 같은 소스를 본다.
STRATEGY_NAMES: tuple[str, ...] = ("sma_crossover", "rsi_reversion")
StrategyName = Literal["sma_crossover", "rsi_reversion"]


class StrategyProfile(BaseModel):
    """단일 종목·전략 묶음. YAML 한 파일이 한 프로파일."""

    name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    strategy: StrategyName
    params: dict[str, int | float] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: str = ""
    created_at: datetime
    last_advised_at: datetime | None = None

    @classmethod
    def load_yaml(cls, path: Path) -> StrategyProfile:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"YAML 최상위는 매핑이어야 합니다: {path}")
        return cls.model_validate(raw)

    def dump_yaml(self, path: Path) -> None:
        """한국어 헤더 주석을 포함해 저장. 사용자가 어떤 숫자를 만져도 되는지 안내."""
        path.parent.mkdir(parents=True, exist_ok=True)
        body = self.model_dump(mode="json")
        header = (
            "# auto-trade 자동 생성 — 숫자만 살짝 바꿔도 됩니다.\n"
            f"# 다시 추천 받으려면: uv run auto-trade strategy suggest --symbol {self.symbol}\n"
            "# backtest 실행:    uv run auto-trade backtest --profile <이 파일 경로>\n"
            "# paper 실행:       uv run auto-trade paper --profile <이 파일 경로> --dry-run\n"
        )
        text = yaml.safe_dump(
            body,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        path.write_text(header + text, encoding="utf-8")


__all__ = ["STRATEGY_NAMES", "StrategyName", "StrategyProfile"]
