# TB Research Portal — Python Edition

결핵(TB) 연구 통합 데이터 플랫폼(TBRS Platform)의 Python 구현판입니다.
원본 React 프로토타입(`prj-tb-prototype-try-b`)과 같은 화면·기능을 **Python 가상환경만으로**,
**실제 DB(SQLite, WAL)** 에 연동해 제공합니다. Node.js·시스템 패키지 설치가 필요 없습니다.

- Python ≥ 3.10, Django 5.2 LTS, SQLite(WAL), waitress
- 화면: Django 템플릿 + HTMX + Alpine.js + Chart.js (모두 `static/vendor/`에 동봉, CDN 미사용 — 오프라인 동작)
- 한/영 전환: 화면 우상단 토글(세션 저장)

| 화면 | 주소 | 내용 |
|---|---|---|
| 홈 | `/` | 현황 대시보드(모든 수치 DB 집계) — 로그인 없이 열람 |
| 대상자 등록조회 | `/cases` | 케이스 목록·검색·필터 + 상세 7탭(임상·영상·유전체·DST·생화학·타임라인·커스텀) |
| 데이터 탐색(DEPOT) | `/depot` | AND/OR 조건 트리 → 데이터셋, 분석 5탭(Fisher·Welch), 저장 데이터셋 |
| 유전자 분석(GAP) | `/gap` … | 유전체 브라우저·약제내성 예측(카탈로그 규칙)·GWAS(DST 표현형 Fisher)·유사도·변이 검색 |
| 영상 분석(RAP) | `/rap` … | CXR·CT·PET-CT 뷰어, 타임시리즈, 데이터 조건, 전문가 소견(DB 저장) |
| 데이터신청 | `/data` | 데이터셋 카탈로그·REST API(키)·데이터 접근 신청(DAR)·내 업로드 |
| 데이터 정의 | `/schemas`, `/dictionary` | 커스텀 데이터 구조 탐색·빌더·데이터 입력, 표준 데이터사전(AI 검색 도우미 — 규칙 기반) |
| 데이터 업로드 | `/upload` | 임상·레지멘·검사실·유전체·영상 업로드 → 검증·비식별화 점검 → 관리자 승인 시 반영 |
| 사용자 매뉴얼 | `/manual` | 원본 매뉴얼(아코디언·검색·목차·한/영) — 내용은 DB, Python 판 동작에 맞게 교정 |
| 관리 | `/admin` … | 대시보드·데이터 검증·데이터사전 관리·데이터 구조 관리·데이터 접근 신청·삭제 요청 (관리자 역할) |

## 설치 · 실행

모든 의존성은 프로젝트 안의 `.venv/` 에만 설치됩니다(`sudo`·전역 pip 불필요).

### Linux / macOS

```sh
./setup.sh              # Python 3.10+ 확인 → .venv 생성 → requirements 설치 → DB 마이그레이션·시드(bootstrap)
./run.sh                # 개발 서버  http://127.0.0.1:4363  (이 PC 에서만 접속)
./run.sh 4391           # 포트 지정
./run.sh prod           # 운영 모드(waitress, TB_DEBUG=0, 0.0.0.0 바인딩)
./run.sh prod 4391
```

### Windows

```bat
setup.bat
run.bat
run.bat 4391
run.bat prod
run.bat prod 4391
```

`setup.bat` 은 `py -3`(Python 런처)가 있으면 그것을, 없으면 `python` 으로 가상환경을 만듭니다.
Python 3.10 미만이면 안내 메시지를 내고 중단합니다.

### 스크립트 없이 수동으로

`setup`/`run` 스크립트가 하는 일은 아래 명령과 같습니다(핵심 로직은 모두 `manage.py` 명령).

```sh
python3 -m venv .venv                       # Windows: py -3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt    # Windows: .venv\Scripts\python.exe -m pip install -r requirements.txt
.venv/bin/python manage.py bootstrap        # 마이그레이션 + 시드(최초 1회)
.venv/bin/python manage.py rundev 4391      # 개발 서버 / 운영: manage.py serve 4391
```

### 포트 변경

기본 포트는 4363 입니다. 이미 사용 중이면(예: 같은 PC 에서 원본 React 프로토타입의 Vite 가 4363 을 쓰는 경우)
인자나 환경변수로 바꿉니다. 우선순위: **인자 > `TB_PORT` > 4363**.

```sh
./run.sh 4391                 # 또는
TB_PORT=4391 ./run.sh
```

```bat
set TB_PORT=4391
run.bat
```

### 개발 서버를 다른 PC 에서 접속하게 하려면

개발 서버(`run.sh` / `run.bat`)는 디버그 화면 노출을 막기 위해 기본으로 `127.0.0.1` 에만 바인딩합니다.
같은 네트워크에서 접속해야 할 때만 `TB_BIND=0.0.0.0 ./run.sh` 로 실행하세요(DEBUG 상태로 외부에 열면 경고가 출력됩니다).
여러 사람이 쓰는 환경에서는 운영 모드(`run.sh prod`)를 쓰세요.

### 운영 전제와 외부 공개

이 포털은 **내부망 HTTP 운영**을 전제로 합니다(`TB_ALLOWED_HOSTS` 기본값 `*`).
인터넷 등 외부에 공개할 때는 반드시
1. HTTPS 리버스 프록시(nginx, IIS 등) 뒤에서 `run.sh prod` 를 실행하고,
2. `TB_ALLOWED_HOSTS=portal.example.org` 처럼 허용 호스트를 지정하세요.

`TB_ALLOWED_HOSTS` 없이 운영 모드를 띄우면 경고가 출력됩니다.
**DEBUG(`run.sh` 개발 모드)는 로컬 전용**입니다 — 오류 화면에 코드·지역 변수(DB 접속 인자 포함)가 보이므로 외부에 열지 마세요.

#### 요청 크기 (대용량 업로드)

- 시퀀스·영상 파일은 화면이 **파일 1개 = 요청 1개**로 차례로 보냅니다(진행률 표시, 실패한 파일만 재시도). 서버도 요청당 파일 1개만 받습니다.
- 운영 서버(waitress)는 요청 본문을 앱에 넘기기 전에 버퍼링합니다. 본문 상한은 **단일 파일 최대(CT/PET-CT 2GB) + 16MB**이고,
  임시 버퍼는 `var/tmp/` 에 둡니다. 더 낮추려면 `TB_MAX_REQUEST_BYTES=바이트수` 를 지정합니다(올릴 수는 없음).
- waitress 단독으로는 경로별 상한을 둘 수 없으므로 로그인하지 않은 요청도 이 상한까지 버퍼링될 수 있습니다.
  **외부에 공개할 때는** 리버스 프록시에서 경로별로 제한하세요 — 로그인·일반 경로 10MB, 업로드 파일 경로만 대용량:

```nginx
server {
    listen 443 ssl;
    server_name portal.example.org;
    client_max_body_size 10m;                      # 로그인·일반 화면
    client_body_temp_path /var/lib/nginx/body;

    location ~ ^/upload/(genomic|imaging)/files/?$ {   # 시퀀스·영상 파일(요청당 1개)
        client_max_body_size 2064m;
        proxy_request_buffering off;               # 프록시에서 통째로 쌓지 않고 바로 넘긴다
        proxy_read_timeout 600s;
        proxy_pass http://127.0.0.1:4363;
    }
    location ~ ^/upload/[a-z]+/csv/?$ {            # CSV (최대 50MB)
        client_max_body_size 51m;
        proxy_pass http://127.0.0.1:4363;
    }
    location / {
        proxy_pass http://127.0.0.1:4363;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### 업로드 사용자 안내

- `condition_id`·`specimen_id` 는 영문·숫자·`-`·`_`·`.` 만 쓸 수 있습니다(공백·한글·특수문자는 오류).
- DICOM 은 헤더를 끝까지 판독해 환자 식별 태그를 확인합니다. Deflate 압축 데이터셋, 잘린 파일, 지나치게 깊거나 큰 헤더처럼
  **판독할 수 없는 DICOM 은 식별정보 부재를 확인할 수 없으므로 제출이 차단**됩니다 — 익명화 도구로 비압축(Explicit/Implicit VR) 형식으로 다시 저장해 올리세요.

### 환경변수 (선택)

| 이름 | 기본값 | 설명 |
|---|---|---|
| `TB_DEBUG` | `1` (prod 모드는 `0`) | 디버그 모드 |
| `TB_PORT` | `4363` | 서버 포트 (실행 인자가 우선) |
| `TB_BIND` | 개발 `127.0.0.1` / prod `0.0.0.0` | 바인딩 주소 |
| `TB_SECRET_KEY` | — | 없으면 `var/secret_key`(권한 0600)를 자동 생성·재사용 |
| `TB_ALLOWED_HOSTS` | `*` | 쉼표 구분 허용 호스트 — 외부 공개 시 필수 |
| `TB_MAX_REQUEST_BYTES` | 2GB+16MB | 요청 본문 상한(waitress·업로드 핸들러) — 낮추는 것만 가능 |
| `TB_DATABASE_URL` | — | 지정하면 PostgreSQL 사용(아래 'PostgreSQL 로 전환') |
| `TB_DB_LOCK_TIMEOUT_MS` | `5000` | PostgreSQL 잠금 대기 상한(밀리초, 1 이상 — 0 은 무제한 대기라 거부) |

#### `var/.env` — 로컬 설정 파일

위 환경변수를 매번 지정하지 않으려면 `var/.env` 에 적어 둡니다(`var/` 는 `.gitignore` 대상이라 **저장소에 올라가지 않습니다** —
DB 비밀번호 같은 값을 여기에 둡니다. Linux 에서는 `chmod 600 var/.env` 권장). 설정을 읽을 때(`manage.py` 모든 명령·`pytest`·서버) 자동으로 읽습니다.

```sh
# 주석은 줄 맨 앞 # 만
TB_DATABASE_URL=postgresql://tbrs:비밀번호@127.0.0.1:5432/tbrs
TB_ALLOWED_HOSTS="portal.example.org"
```

- 한 줄에 `KEY=VALUE`. 값은 따옴표(`"…"`/`'…'`)로 감쌀 수 있고(벗겨서 그대로 사용, 이스케이프 처리 없음), 앞뒤 공백은 잘립니다.
  따옴표 없는 값의 `#` 은 값의 일부입니다(줄 중간 주석 없음). 앞의 `export ` 는 허용합니다. 파일은 UTF-8(BOM 허용).
- **이미 지정된 환경변수가 항상 우선**합니다 — 파일은 비어 있는 자리만 채웁니다. 빈 값으로 지정한 것도 '지정됨'으로 봅니다.
- 형식이 틀린 줄(`=` 없음, 잘못된 이름, 닫히지 않은 따옴표, 같은 이름 두 번)은 무시하지 않고 줄 번호와 함께 오류로 멈춥니다(값은 출력하지 않음).
- 파일을 지우면(또는 `TB_DATABASE_URL` 줄을 빼면) 기본값 **SQLite**(`var/db.sqlite3`)로 돌아갑니다.
- `var/.env` 가 PostgreSQL 을 가리키는 상태에서 한 번만 SQLite 로 실행하려면 `TB_DATABASE_URL` 을 **빈 값**으로 지정합니다:
  - Linux: `TB_DATABASE_URL= .venv/bin/pytest` / `TB_DATABASE_URL= ./run.sh 4391`
  - Windows(cmd): `set "TB_DATABASE_URL="` 는 변수를 **지우는** 것이라 효과가 없으므로, 공백 하나를 넣어
    `set "TB_DATABASE_URL= "` 후 실행합니다(앞뒤 공백은 잘려 빈 값과 같음). PowerShell: `$env:TB_DATABASE_URL=" "`.

## 데모 계정

| 아이디 | 비밀번호 | 역할 |
|---|---|---|
| `admin` | `admin1234` | 관리자 |
| `dmanager` | `dmng1234` | 데이터관리자(관리자 권한) |
| `researcher` | `res1234` | 연구원 |

- 홈(`/`)만 비로그인 열람 가능, 나머지 화면은 로그인 필요(로그인 후 원래 화면으로 복귀).
- `/admin/*` 포털 관리 화면은 관리자 역할(admin, dmanager) 필요(`/admin/login`).
- 프로필(`/profile`)에서는 이름·이메일만 바꿀 수 있습니다. **소속 기관은 읽기 전용**이며 관리자만 `/django-admin/` 에서 바꿉니다
  (원본 Profile.tsx 는 본인 수정 가능 — 소속 기관이 '기관 공유' 데이터 구조의 열람 범위를 정하므로 의도적으로 막았습니다).
- Django 기본 관리 사이트는 `/django-admin/` 입니다(`admin` 계정만 접근 가능).

## 관리 명령

가상환경의 python 으로 실행합니다(`.venv/bin/python` / `.venv\Scripts\python.exe`).

```sh
python manage.py bootstrap         # migrate → 데모 계정 → 표준 데이터사전 → 환자 시드 → 홈 콘텐츠 → 저장 데이터셋
                                   #   → 샘플 데이터 구조 → 변이 카탈로그 → 공개 샘플 영상 → 사용자 매뉴얼
python manage.py seed_demo         # 데모 계정 + 환자 시드 (--accounts-only: 계정만, --force: 다시 적재)
python manage.py load_dictionary   # 통합 데이터사전 엑셀 적재 (--force: 다시 반영, --path: 다른 엑셀)
python manage.py load_schemas      # 샘플 커스텀 데이터 구조 적재(구조 4·필드 48·입력 행 14, bootstrap 이 1회 적재)
                                   #   원본 행의 연결 키 COND-2024-KR-0001 은 시드 환자 형식 KR-2024-0001 로 바꿔 적재
                                   #   (시드 환자에 없는 6건은 연결되지 않음 — 출력에 표시). schema-002 의 sample_id 는
                                   #   고유값 필드지만 원본 데이터에 중복(RNA-KR-001 2건)이 있어 그대로 둔다.
python manage.py load_manual       # 사용자 매뉴얼(data/seed/manual.json) 1회 적재 (--force: 시드 내용으로 다시 맞춤)
python manage.py cleanup_media     # 지우지 못한 업로드 파일·DB 가 참조하지 않는 파일 정리
python manage.py rundev [port]     # 개발 서버, 기본 127.0.0.1:4363 (TB_PORT, TB_BIND) — run.sh 가 사용
python manage.py serve [port]      # waitress 운영 서버, DEBUG=0 (--host, --threads, TB_PORT, TB_BIND) — run.sh prod 가 사용
python manage.py runserver 4363    # Django 기본 개발 서버(127.0.0.1)
pytest                             # 테스트 (.venv/bin/pytest)
TB_PERF=1 pytest apps/uploads/tests/test_deletion_perf.py -s   # 삭제 재적용 성능 측정(기본 실행에서는 건너뜀)
```

**시드 적재 규칙**: 데이터셋(accounts, dictionary, patients, home_content, cohorts, schemas, genomics_catalog,
imaging_samples, manual)마다 최초 1회만 적재하고 `SeedLog` 에 기록합니다.
`setup`/`bootstrap` 을 다시 실행해도 이미 적재한 데이터셋은 건너뛰므로, 사용자가 지운 계정·환자가 되살아나지 않습니다.
다시 넣으려면 `--force` 를 씁니다.
- `seed_demo --force`: 없는 계정·환자만 다시 만듭니다(기존 레코드는 수정하지 않음).
- `load_dictionary --force`: 항목 ID(UDE) 기준으로 갱신합니다. 엑셀에서 사라진 항목은 지우지 않고 폐기(`is_deprecated`) 표시합니다.
  다른 버전의 엑셀이면 새 버전으로 추가되어 현행이 되고, 이전 버전 행은 그대로 남습니다.
  **주의:** `--force --path <옛 버전 엑셀>` 로 적재하면 그 옛 버전이 현행(화면에 보이는 버전)으로 바뀝니다.
  현행으로 되돌리려면 최신 엑셀로 다시 `load_dictionary --force` 를 실행하세요.

## 구조

```
config/              settings(경로는 pathlib, DB·업로드·비밀키는 var/), urls, wsgi
apps/core/           레이아웃(헤더·푸터·사이드바·토스트·언어 토글), 메뉴 정의, t() 헬퍼·템플릿 태그,
                     bootstrap·serve 명령
apps/accounts/       커스텀 User(role: researcher|admin), 로그인/관리자 로그인/로그아웃/프로필,
                     login_required·admin_required 데코레이터, seed_demo 명령
apps/dictionary/     표준 데이터사전(버전·카테고리·항목) + 엑셀 적재 서비스(원본 gen_dict.py 규칙), load_dictionary 명령,
                     관리 → 데이터사전 관리(/admin/dictionary: 항목·카테고리 CRUD·폐기, 엑셀 업로드로 버전 적재, 변경 이력)
apps/cases/          환자 케이스(Patient + DST 결과) + 시드 적재
apps/schemas/        데이터 정의(/schemas): 커스텀 데이터 구조(Schema·SchemaField·SchemaRecord) 탐색·빌더·상세·데이터 입력
                     (웹 폼·CSV·기존 데이터 수정, 값 검증은 서버), 관리 → 데이터 구조 관리(/admin/schemas), load_schemas 명령
apps/cohorts/        데이터 탐색(/depot): 조건 트리(AND/OR 중첩, 허용 목록·깊이·노드 상한) → ORM 쿼리, 분석 5탭,
                     저장 데이터셋(소유자만 이름 변경·삭제·CSV, 목록은 전체 공개), cohorts 시드(bootstrap 이 1회 적재)
apps/uploads/        데이터 업로드(/upload) 5종 → 검증·비식별화(PII·DICOM 헤더) → 관리자 검증(/admin/review) 승인 시 반영,
                     내 업로드·삭제 요청(/admin/deletions — 환자×도메인 재적용으로 되돌림), cleanup_media 명령
apps/datasharing/    데이터신청(/data): 카탈로그(DB 집계)·DAR(관리자 승인 /admin/dar)·승인 게이트 CSV/엑셀 내보내기,
                     REST API(/api/v1 — 키 해시 저장, 요청 제한)
apps/genomics/       유전자 분석(/gap): 내성 유전자·변이 카탈로그(시드), 약제내성 예측 규칙·실행 기록, GWAS·유사도 계산
apps/imaging/        영상 분석(/rap): 공개 샘플 영상 자산·검사 시드, 전문가 소견(DB), 업로드 영상 권한 검사 서빙
apps/manual/         사용자 매뉴얼(/manual): 대주제·소주제·블록(DB), 검색, load_manual 명령
templates/           base.html, partials/(header, footer, sidebar, toasts, lang_toggle)
static/css/app.css   디자인 토큰(CSS 변수) + 공통 컴포넌트 클래스(card, btn, badge, input, table, tabs, modal, stat-card …)
static/js/app.js     토스트·도움말 툴팁
static/vendor/       htmx, Alpine.js, Chart.js (버전·출처: VERSIONS.md)
static/icons/        lucide SVG (필요한 것만) — 템플릿에서 {% icon "name" "class" %}
static/samples|imaging|manual/   원본 public/ 자산
data/seed/           통합 데이터사전 엑셀(v3.2.0), 원본 Mock 을 변환한 JSON
scripts/             convert_mock_ts.py — 원본 src/data/*.ts → data/seed/*.json (일회성, Node 불필요)
                     convert_manual_ts.py — 원본 매뉴얼 → data/seed/manual.json (+ manual_overrides.json 교정 적용)
var/                 런타임 산출물(db.sqlite3, media/, tmp/ 업로드 임시, secret_key) — git 제외
```

홈의 뉴스·게시판·공지·연구사업·관련 사이트는 DB(apps.core.models)에 있으며 `/django-admin/` 에서 편집합니다.
업로드 승인 시 반영 규칙: 오류 행은 반영하지 않고, 경고 행의 규칙 위반 값은 버립니다(검토 화면 '버린 필드').
기존 환자의 임상 갱신은 **CSV 헤더에 있는 컬럼의, 값이 있는 칸만** 반영하며 **빈 칸은 기존 값을 유지**합니다(지우지 않음).
같은 파일(SHA-256)·생화학 결과·레지멘을 다시 승인해도 중복 생성하지 않습니다.
지우지 못한 업로드 파일(Windows 파일 잠금 등)과 DB 가 참조하지 않는 파일은 `python manage.py cleanup_media` 로 정리합니다.
업로드 크기 제한은 `config/settings.py` 의 `UPLOAD_LIMITS`(CSV 50MB, 시퀀스 500MB, CXR 50MB, CT/PET-CT 2GB)입니다.

다국어: 템플릿 `{% t "한국어" "English" %}`, 파이썬 `t(request, "한국어", "English")`,
DB 병기 컬럼은 `{% locf obj "name" %}` (영어일 때 `name_en`).

## 업그레이드 주의

0단계 리뷰 수정 이전(시드 적재 기록 `SeedLog` 도입 전)에 만든 `var/` 가 있다면, 마이그레이션 이력이 다르므로
`var/` 를 삭제한 뒤 `setup.sh` / `setup.bat` 을 다시 실행하세요. (이후 모델 변경은 항상 새 마이그레이션으로 추가됩니다.)

## 다른 PC 로 옮기기 (이식)

1. 프로젝트 폴더를 복사합니다. **`.venv/` 는 복사하지 않습니다**(OS·경로에 묶여 있음).
   `var/` 를 함께 복사하면 DB·업로드 파일이 그대로 옮겨지고, 빼면 새 DB 로 시작합니다.
2. 대상 PC 에서 `setup.sh`(Linux) 또는 `setup.bat`(Windows) 를 실행해 가상환경을 다시 만듭니다.
3. `run.sh` / `run.bat` 으로 실행합니다.

## 동시 쓰기와 DB 잠금 (503 안내)

SQLite(WAL)는 쓰기를 한 번에 하나만 처리합니다. 다른 쓰기가 `busy_timeout`(5초)보다 오래 DB 를 잡고 있으면
(예: 큰 업로드의 승인·삭제 승인) 그 사이의 쓰기 요청은 처리되지 않습니다. 이때 화면은 500 오류 대신 **503 '잠시 후 다시 시도'**
안내를 보여 줍니다(`apps.core.middleware.DatabaseBusyMiddleware` — 일반 화면은 안내 페이지, HTMX 요청은 알림 토스트,
API·fetch·업로드 XHR 은 JSON `{"status": "db_busy"}`, 모두 `Retry-After: 5`). 저장되지 않았으므로 같은 작업을 다시 하면 됩니다.
잠금이 아닌 DB 오류는 그대로 500 으로 드러납니다(결함은 숨기지 않음). 세션 저장(응답 단계)에서 나는 잠금은 이 처리 밖입니다.

'저장되지 않았습니다'가 참이 되도록, 쓰기를 여러 번 나눠 하던 요청(업로드 마법사의 임시저장 생성 + 데이터/파일 저장 + 단계 이동,
데이터 구조 입력 검사, 데이터사전 엑셀 적재, `POST /api/v1/cohorts`)은 **요청 1건 = 커밋 1번**으로 묶었습니다(`apps.core.filetx.atomic_write`).
중간에 실패하면 DB 쓰기가 모두 롤백되고, 그 요청이 새로 저장한 파일도 지웁니다(지우지 못한 파일은 `cleanup_media` 가 정리).
기존 파일의 삭제는 DB 삭제가 커밋된 뒤에만 하며, 그 정리가 실패해도(파일 잠금, 실패 기록조차 못 남기는 DB 잠금) 이미 커밋된
요청의 응답과 새 파일은 그대로입니다 — 남은 파일은 DB 가 참조하지 않으므로 `python manage.py cleanup_media` 가 회수합니다. 예외: API 키의 '마지막 사용 시각' 기록은 인증 단계의 별도 쓰기입니다.
SQLite 는 쓰기 트랜잭션을 시작할 때 잠금을 잡으므로(IMMEDIATE) 잠금 오류는 첫 쓰기 전에만 날 수 있고, PostgreSQL 은 행·테이블 잠금이
중간에 날 수 있지만 같은 트랜잭션이라 함께 롤백됩니다. 업로드 승인·삭제 승인은 자체 잠금 순서와 재시도 구조가 있어 전역
`ATOMIC_REQUESTS` 는 쓰지 않습니다.

## PostgreSQL 로 전환

ORM 만 사용하므로 코드 수정 없이 **설정(환경변수 또는 `var/.env`)으로만** 전환합니다. 지정하지 않으면 기본값 SQLite(`var/db.sqlite3`) 그대로입니다.

**검증 결과(PostgreSQL 16.13, psycopg 3.3.9, Linux)**: 빈 DB 에 `migrate`·`bootstrap`(재실행 시 모든 데이터셋 건너뜀 — 멱등),
`makemigrations --check`(변경 없음), 전체 `pytest` 를 PostgreSQL 로 실행해 통과(1832 통과, 2 건너뜀 — `TB_PERF=1` 성능 측정 전용).
실제 경합으로 확인한 것: 잠금 대기 초과(`lock_timeout`)·교착(deadlock)·직렬화 실패 → 503 안내, 승인 순번(`approval_seq`) 동시 채번 재시도,
삭제 승인·승인 동시 실행(환자 id 순 잠금), JSON 조회(`has_any_value` → `jsonb ?|`, 고유값 검사 `KeyTransform`), SQLite → PostgreSQL
`dumpdata`/`loaddata` 후 시퀀스(자동 재설정 확인). Windows 에서의 PostgreSQL 실행은 검증하지 않았습니다(드라이버는 Windows wheel 제공).

1. 드라이버(선택 의존성)를 가상환경에 설치합니다 — 기본 `requirements.txt` 에는 없습니다:
   `.venv/bin/python -m pip install -r requirements-postgres.txt` (Windows: `.venv\Scripts\python.exe -m pip install -r requirements-postgres.txt`)
2. `var/.env` 에 적거나(위 '`var/.env` — 로컬 설정 파일') 환경변수로 지정합니다:

| 이름 | 예 | 설명 |
|---|---|---|
| `TB_DATABASE_URL` | `postgresql://tbrs:비밀번호@127.0.0.1:5432/tbrs` | 지정하면 PostgreSQL 사용(비밀번호의 특수문자는 URL 인코딩, 예 `@` → `%40`). 빈 값이면 SQLite |
| `TB_DB_LOCK_TIMEOUT_MS` | `5000` (기본) | 잠금 대기 상한(밀리초, 1 이상) — 넘으면 503 '잠시 후 다시 시도' 안내(SQLite `busy_timeout` 과 같은 값). `0`(PostgreSQL 의 '무제한 대기')은 503 안내가 동작하지 않으므로 거부합니다 |

URL 쿼리 문자열로는 libpq 접속 옵션 `sslmode`·`sslrootcert`·`sslcert`·`sslkey`·`connect_timeout`·`application_name` 만 받습니다
(예: `postgresql://tbrs:비밀번호@db.example.org:5432/tbrs?sslmode=verify-full&sslrootcert=/etc/ssl/ca.pem`). 그 밖의 이름(오타, `options` 등)은
설정 오류로 멈춥니다. DEBUG 오류 화면에는 `TB_DATABASE_URL` 등 접속 정보 계열 값이 가려져 나옵니다(`apps.core.debug`).

3. 빈 DB 에 `python manage.py bootstrap` (마이그레이션 + 시드, 여러 번 실행해도 안전).
4. 기존 SQLite 데이터를 옮기려면(대상 PostgreSQL DB 는 `python manage.py migrate` 만 한 빈 상태):
   ```sh
   TB_DATABASE_URL= python manage.py dumpdata --natural-foreign --natural-primary \
       -e contenttypes -e auth.permission -e sessions -e admin.logentry -o var/dump.json   # SQLite 에서 내보내기
   python manage.py loaddata var/dump.json                                                # PostgreSQL 로 (var/.env 설정 상태)
   python manage.py bootstrap                                                             # 빠진 시드만 채움(SeedLog 기준)
   ```
   `loaddata` 가 PostgreSQL 의 id 시퀀스를 자동으로 맞춥니다(별도 `sqlsequencereset` 불필요 — 검증 시 전 모델 확인).
   업로드 파일(`var/media/`)은 DB 와 함께 그대로 복사합니다.
5. `pytest` 를 같은 설정으로 실행해 확인합니다(DB 사용자에게 테스트 DB `test_<이름>` 생성 권한 필요).
   `transaction=True` 동시성 테스트는 테이블 비우기(TRUNCATE) 때문에 SQLite 보다 훨씬 느립니다(검증 환경에서 전체 약 8분, SQLite 약 4분).
   PostgreSQL 에서만 재현되는 테스트(SQLSTATE 판별·실제 교착·`approval_seq` 경합, 6개)는 SQLite 실행 시 사유와 함께 건너뜁니다.

**SQLite 와 남은 차이**

- **NUL 문자(`\x00`)**: PostgreSQL 은 text·jsonb 에 NUL 을 저장·비교하지 못합니다. 두 백엔드에서 같은 결과가 되도록 처리합니다:
  관리자·사용자 자유 텍스트(업로드 검토 의견·반려/수정 사유·삭제 요청 반려 사유·비식별화 서명, DAR 승인 의견·반려 사유,
  데이터 구조 상태 변경 메모)는 공통 검사(`apps.core.textinput`)로 **오류 안내 후 거부**, Django 폼·사용자 JSON 은 기존처럼 거부,
  업로드 웹 폼 값은 NUL 을 지운 뒤 행 검증, 검색어는 일치 없음, 업로드 ID 등 식별자는 404.
  새 화면을 만들 때도 같은 규칙을 지켜야 합니다 — 모든 URL 패턴(인자는 실제 객체 값)의 GET·POST 입력 이름 각각에 NUL 을 넣는
  퍼징 하네스(`apps/core/tests/test_nul_fuzz.py`)가 두 백엔드에서 500 이 없음을 확인합니다.
- **문자열 길이**: PostgreSQL 은 `max_length` 를 DB 에서 강제합니다(SQLite 는 무시). 폼·서비스가 이미 길이를 검사하므로 화면 동작은 같습니다.
- **정렬 순서**: 이름순 정렬은 DB 콜레이션을 따릅니다(검증 DB `en_US.utf8`, SQLite 는 바이트 순) — 영문 대소문자·기호가 섞인 이름의 순서가 다를 수 있습니다.
- **JSON 키 순서**: jsonb 는 객체 키 순서를 보존하지 않습니다(저장 시 정렬). JSON 객체의 키 순서에 의존하는 코드는 찾지 못했고(목록은 배열, 표시는 필드 정의 순서) 테스트에서도 차이가 없었습니다.
- **잠금 단위**: SQLite 는 쓰기 하나씩(IMMEDIATE), PostgreSQL 은 행 단위라 서로 다른 환자의 승인은 동시에 진행됩니다(번호 경합은 재시도로 처리).
- **id**: 테스트·삭제 후 id 가 1부터 다시 시작하지 않습니다(시퀀스) — id 값에 의미를 두지 않습니다.

## 사용자 매뉴얼

`/manual` 은 원본 매뉴얼(`src/pages/Manual/manualContent.ts`)을 `scripts/convert_manual_ts.py` 로 변환한 내용을 DB 에서 보여 줍니다.
원본 문구 중 이 포털의 실제 동작과 다른 부분(브라우저 세션 저장 → 서버 DB 저장, 약제내성 예측은 카탈로그 기반 규칙, 유사도 방법명,
GWAS 는 DST 표현형, DAR 은 관리자 승인, DICOM 헤더 판독 등)은 `data/seed/manual_overrides.json` 에 이유와 함께 교정해 두었습니다
(40건). 화면 캡처(`static/manual/`)는 원본 프로토타입의 것이라 세부 배치가 조금 다를 수 있습니다.
내용은 `/django-admin/` 에서 고칠 수 있고, `load_manual --force` 는 시드 내용으로 되돌립니다.

## 알려진 한계 (수용)

- **삭제 재적용 비용**: 업로드 삭제 승인은 영향 (환자, 도메인)의 남은 승인 업로드를 다시 적용합니다. 대상 환자 행만 읽도록 줄였지만
  (5단계), 비용은 그 환자들에 반영된 업로드 행 수에 비례합니다 — 1000행×8 업로드(환자 50명 전체에 걸침) 삭제 약 1.5초.
  그동안 다른 쓰기는 대기하며, 5초를 넘으면 503 안내가 나옵니다.
- **변이 집계 캐시 없음**: 유전자 분석의 빈도·샘플 수는 요청마다 전체 환자의 변이 기록을 훑어 계산합니다(수만 명 규모에서는 느려질 수 있음).
- **업로드 영상 메타데이터 공개 범위**: 업로드 영상의 픽셀·원 파일명·전문가 소견 내용은 제출자·관리자·영상 DAR 승인자만 보지만,
  촬영일·모달리티·판독 소견(메타데이터)은 로그인 사용자 전체에게 보입니다.
- **DICOM 미리보기 없음**: 영상 뷰어는 JPG·PNG 만 표시합니다(DICOM 은 헤더 판독·비식별 점검만).
- **요청 제한은 프로세스 메모리**: API 키당·인증 실패 IP당 제한은 프로세스 안 캐시라 여러 프로세스로 띄우거나 프록시 뒤에서 IP 가
  하나로 묶이면 정확하지 않습니다(프록시에서 따로 제한하세요).
- **전문가 소견 상한(검사당 500건)** 은 동시에 추가하면 근소하게 넘을 수 있습니다.
- **`fabG1` 미편입**: 변이 카탈로그는 원본 20건 기준이라 `fabG1`(inhA 프로모터 c-15t 의 다른 표기)은 별도 항목이 아닙니다.
- **샘플 영상 재시드**: 환자 영상 수만 수동으로 바꾼 뒤 `load_imaging_samples(force=True)` 를 다시 돌리면 공개 샘플 검사가 추가될 수 있습니다.
- **복원 불가 항목**: 업로드 삭제 시 임상 값 덮어쓰기·DST/도말/배양 병합·`last_update_date` 는 되돌리지 않습니다(확인 화면에 안내).
- **PostgreSQL**: PostgreSQL 16 에서 전체 테스트·시드·기동을 검증했습니다(Linux). 남은 차이는 위 'PostgreSQL 로 전환' 절 참고.
