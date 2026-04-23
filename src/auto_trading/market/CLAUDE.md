# market/ 규칙

- 이 폴더는 **읽기 전용**. 주문·잔고 변경 코드를 두지 않는다.
- 외부에 노출하는 값은 도메인 타입(`Bar`, `Quote`, `Tick`)으로 변환해서 리턴 — 키움 원본 dict 를 그대로 반환하지 말 것.
- 시간은 항상 `datetime` (KST, `ZoneInfo("Asia/Seoul")`) 사용. 문자열 날짜 전달 금지.
