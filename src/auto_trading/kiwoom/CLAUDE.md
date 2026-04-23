# kiwoom/ 규칙

- 베이스 URL / WS URL 은 `config.Settings` 프로퍼티에서만 읽는다. 하드코딩 금지.
- AppKey·Secret·토큰을 로그로 출력하지 않는다. 디버그 시에도 앞 4자 + `…` 마스킹.
- TR ID / URL 경로는 `endpoints.py` 상수로만 사용 — 문자열 직접 박지 말 것.
- 요청/응답 형태가 바뀌면 `schemas.py` 의 pydantic 모델부터 수정해 테스트 깨지는 지점을 드러낸다.
- 토큰은 `auth.TokenProvider` 통해서만 취득. 다른 곳에서 `/oauth2/token` 호출 금지.
