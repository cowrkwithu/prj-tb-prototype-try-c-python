# TB Research Portal — Python Edition (prj-tb-prototype-try-c-python)

원본 `../prj-tb-prototype-try-b` (React 19 + TS, Mock 데이터·sessionStorage 기반 UI 프로토타입)를
**Python 가상환경만으로 동작하고, 실제 DB와 연동되는** 동일 기능의 웹 포털로 재구현한다.

## 절대 요구사항 (위반 시 리뷰 불합격)
1. **시스템 무영향**: 모든 의존성은 프로젝트 내 `.venv`에만 설치. `sudo`, 전역 `pip install`, 시스템 패키지 설치 금지.
2. **Node.js 금지**: 빌드·실행 어디에도 Node/npm 불필요. 프론트 JS 라이브러리는 `static/vendor/`에 파일로 동봉(CDN 금지, 오프라인 동작).
3. **이식성(Windows + Linux)**: 경로는 `pathlib`, 셸 전용 기능 의존 금지, 파일 인코딩은 항상 `encoding="utf-8"` 명시.
   OS별 스크립트는 쌍으로 제공(`setup.sh`/`setup.bat`, `run.sh`/`run.bat`), 핵심 로직은 Python(`manage.py` 커맨드)에 둔다.
   gettext(msgfmt) 같은 시스템 도구가 필요한 기능 사용 금지.
4. **실제 동작**: 화면의 모든 데이터는 DB에서 읽고, 생성/수정/승인/삭제는 DB에 영속된다. Mock/sessionStorage 대체 금지.

## 기술 스택 (확정)
- Python ≥ 3.10, Django 5.2 LTS, SQLite(WAL) — ORM만 사용(raw SQL 지양)해 PostgreSQL 전환은 `DATABASES` 설정만으로 가능하게.
- 화면: Django 템플릿 + HTMX + Alpine.js + Chart.js (모두 `static/vendor/`에 고정 버전 동봉).
- CSS: Tailwind 빌드 없이 `static/css/app.css` 한 벌(디자인 토큰 CSS 변수 + 컴포넌트 클래스).
- 엑셀: openpyxl. 운영 서버: waitress (Windows/Linux 공통). 테스트: pytest + pytest-django.
- 의존성은 `requirements.txt`에 `==` 버전 고정.

## 디자인 시스템 (원본 계승)
Primary Navy `#1B3A5C`, Secondary Teal `#2A7F8E`, Background `#F8FAFC`, Surface `#FFFFFF`,
Text `#1E293B` / `#64748B`, Border `#E2E8F0`, Success `#16A34A`, Warning `#EAB308`, Danger `#DC2626`, Info `#2563EB`.
Radius: 카드 8px, 버튼 6px, 입력 4px. 폰트: Pretendard → 시스템 한글 폰트 폴백. 반응형(모바일 햄버거 메뉴).

## 다국어 (한/영)
원본처럼 화면 우상단 토글로 ko/en 전환(세션 저장). Django gettext/.po 대신
템플릿 태그 `{% t "한국어" "English" %}` 와 파이썬 헬퍼 `t(request, ko, en)` 사용. DB 데이터는 `*_en` 컬럼 병기.

## 디렉터리 구조
```
config/            # settings, urls, wsgi
apps/<name>/       # Django 앱 (models, views, urls, forms, services, templates/<name>/, tests/)
templates/         # base.html, partials/
static/            # css/, js/, vendor/, img/ (원본 public/ 자산 포함)
data/seed/         # 시드 원본(엑셀 데이터사전, 샘플 CSV, 원본 Mock 데이터를 변환한 JSON)
var/               # 런타임 산출물: db.sqlite3, media(업로드 파일) — git 제외
scripts/           # 일회성 변환 스크립트
docs/              # PLAN.md, 진행 기록
```
앱 목록(예정): core, accounts, cases, dictionary, uploads, review, cohorts(DEPOT), schemas, datasharing, genomics(GAP), imaging(RAP), manual.

## 계정 (원본 데모 계정 재현, `seed_demo`로 생성)
admin/admin1234(관리자), dmanager/dmng1234(데이터관리자, 관리자 권한), researcher/res1234(연구원).
역할: researcher, admin. 홈(`/`)만 비로그인 열람 가능, 나머지는 로그인 필요, `/admin/*` 포털 관리 화면은 admin 역할 필요.
(Django 기본 admin 사이트는 `/django-admin/`으로 이동해 원본 `/admin` 경로와 충돌하지 않게 한다.)

## 표준 명령 (모든 OS 공통 — 가상환경 활성화 후)
```
python manage.py bootstrap      # migrate + 시드 최초 1회 적재(SeedLog로 기록, 재실행 시 건너뜀)
python manage.py rundev [port]  # 개발 서버, 기본 127.0.0.1:4363 (TB_PORT, TB_BIND로 변경)
python manage.py serve [port]   # 운영(waitress, DEBUG=0)
pytest
```
OS 래퍼: `setup.sh|bat`, `run.sh|bat [port]`, `run.sh|bat prod [port]`.
이 PC에서는 원본(try-b) Vite가 4363을 점유하므로 검증용 서버는 다른 포트를 쓰고 반드시 종료한다.

## 코딩 규칙
- 비즈니스 로직은 `services.py`, 뷰는 얇게. 폼 검증은 Django Form/ModelForm.
- 모든 쓰기 요청은 POST + CSRF, 권한 검사는 서버에서(템플릿에서 숨기는 것만으로는 불충분).
- 업로드 파일은 `var/media/` 아래, 크기·확장자 서버 검증, 파일명은 저장 시 재생성.
- 시드 커맨드는 멱등(두 번 실행해도 중복 생성 없음).
- 마이그레이션: 0단계 이후 이미 생성된 마이그레이션 파일은 다시 쓰지 않는다. 모델 변경은 항상 새 마이그레이션(0002…)으로.
- 사용자 입력 JSON은 `json.loads` 대신 `apps.core.safejson.loads_user_json` 사용(고립 서로게이트·NUL 거부). 템플릿 `|safe` 금지, 속성 안 데이터는 json_script/escapejs.
- 사용자 정의 정규식은 `re`가 아닌 timeout 있는 `regex` 로만 실행. 모든 뷰는 어떤 입력에도 500을 내지 않아야 한다(퍼징 테스트로 확인).
- 화면 통계·집계는 DB에서 계산한다(원본 statistics.ts 같은 하드코딩 수치를 그대로 쓰지 않는다).
- 기능마다 pytest 테스트(모델/서비스 + 뷰 응답·권한) 작성. `pytest` 전체 통과가 단계 완료 조건.
- UI 문구는 원본과 동일한 한국어(영문 병기).
