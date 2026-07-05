"""라이브(모의) 매매 루프 패키지.

구현은 `paper.py`(PaperRunner)에 있다. 이 파일은 기존 `from auto_trading.runner
import PaperRunner` 경로를 유지하기 위한 재수출만 담당한다.

주의: 실거래(`KIWOOM_MODE=real`) 에서 러너를 돌리려면 `allow_real=True` 를 명시
지정해야 한다 — 가드 상세는 `paper.py` 참고.
"""

from .aggregator import _BarAggregator as _BarAggregator
from .control import RunnerControlState as RunnerControlState
from .paper import DAY_STATE_MAX_DRIFT as DAY_STATE_MAX_DRIFT
from .paper import PaperRunner as PaperRunner
