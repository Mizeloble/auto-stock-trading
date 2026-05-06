"""전략 플러그인 레지스트리.

```python
@register("sma_crossover")
class SmaCrossover(Strategy): ...
```

CLI 에서 `--strategy sma_crossover` 로 호출 가능. 새 전략 파일만 드롭하면
등록되도록 `load_builtin_strategies()` 에서 import 만 추가하면 된다.
"""

from __future__ import annotations

from typing import Any

from .base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(name: str):
    def _decorate(cls: type[Strategy]) -> type[Strategy]:
        if name in _REGISTRY:
            raise ValueError(f"strategy already registered: {name}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return _decorate


def get(name: str, **params: Any) -> Strategy:
    load_builtin_strategies()
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy: {name}. available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**params)


def available() -> list[str]:
    load_builtin_strategies()
    return sorted(_REGISTRY)


def load_builtin_strategies() -> None:
    """내장 전략 모듈을 import 해 레지스트리에 등록."""
    # 지연 import 로 순환 import 방지
    from . import index_daytrade, intraday, rsi_reversion, sma_crossover  # noqa: F401
