# dashboard/ 규칙

- **read-only**. 파일 쓰기·외부 API 호출 금지. `data/reports/`, `data/backtests/` 만 읽는다.
- **LLM 호출 금지**. 주간 인사이트도 이미 만들어진 JSON 을 단순 렌더만 할 것 (Phase B).
- **로컬 전용**. 기본 host=`127.0.0.1`. `0.0.0.0` 바인딩은 사용자 명시 옵션으로만.
- **시크릿 차단**. 응답에 AppKey/Secret/토큰 절대 포함 금지 — 로더는 화이트리스트 필드만 직렬화.
- **mock/real 무관**. 같은 파일을 읽으므로 모드 분기 없음.
- 프레임워크는 **FastAPI**. 앱은 `app.create_app()` 팩토리로 생성하고 라우트는 `pages.py`(Jinja HTML) 와 `api.py`(JSON) 두 파일로 분리. 템플릿 설정은 `templating.py`, 비즈니스 로직은 `loaders.py` 에만.
- 차트·인터랙션은 모두 CDN(Tailwind/Chart.js/Alpine.js). 빌드 도구·번들러 도입 금지.
