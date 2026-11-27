# TB Research Portal — Python Edition

결핵(TB) 연구 통합 데이터 플랫폼(TBRS Platform)의 Python 구현판입니다.
원본 React 프로토타입(`prj-tb-prototype-try-b`)과 같은 화면·기능을 **Python 가상환경만으로**,
**실제 DB(SQLite, WAL)** 에 연동해 제공합니다. Node.js·시스템 패키지 설치가 필요 없습니다.

- Python ≥ 3.10, Django 5.2 LTS, SQLite(WAL), waitress
- 화면: Django 템플릿 + HTMX + Alpine.js + Chart.js (모두 `static/vendor/`에 동봉, CDN 미사용 — 오프라인 동작)
- 한/영 전환: 화면 우상단 토글(세션 저장)

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
python manage.py bootstrap         # migrate → 데모 계정 → 표준 데이터사전 → 환자 시드 → 홈 콘텐츠
python manage.py seed_demo         # 데모 계정 + 환자 시드 (--accounts-only: 계정만, --force: 다시 적재)
python manage.py load_dictionary   # 통합 데이터사전 엑셀 적재 (--force: 다시 반영, --path: 다른 엑셀)
python manage.py load_schemas      # 샘플 커스텀 데이터 구조 적재(구조 4·필드 48·입력 행 14, bootstrap 이 1회 적재)
                                   #   원본 행의 연결 키 COND-2024-KR-0001 은 시드 환자 형식 KR-2024-0001 로 바꿔 적재
                                   #   (시드 환자에 없는 6건은 연결되지 않음 — 출력에 표시). schema-002 의 sample_id 는
                                   #   고유값 필드지만 원본 데이터에 중복(RNA-KR-001 2건)이 있어 그대로 둔다.
python manage.py rundev [port]     # 개발 서버, 기본 127.0.0.1:4363 (TB_PORT, TB_BIND) — run.sh 가 사용
python manage.py serve [port]      # waitress 운영 서버, DEBUG=0 (--host, --threads, TB_PORT, TB_BIND) — run.sh prod 가 사용
python manage.py runserver 4363    # Django 기본 개발 서버(127.0.0.1)
pytest                             # 테스트 (.venv/bin/pytest)
```

**시드 적재 규칙**: 데이터셋(accounts, dictionary, patients, home_content)마다 최초 1회만 적재하고 `SeedLog` 에 기록합니다.
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
templates/           base.html, partials/(header, footer, sidebar, toasts, lang_toggle)
static/css/app.css   디자인 토큰(CSS 변수) + 공통 컴포넌트 클래스(card, btn, badge, input, table, tabs, modal, stat-card …)
static/js/app.js     토스트·도움말 툴팁
static/vendor/       htmx, Alpine.js, Chart.js (버전·출처: VERSIONS.md)
static/icons/        lucide SVG (필요한 것만) — 템플릿에서 {% icon "name" "class" %}
static/samples|imaging|manual/   원본 public/ 자산
data/seed/           통합 데이터사전 엑셀(v3.2.0), 원본 Mock 을 변환한 JSON
scripts/             convert_mock_ts.py — 원본 src/data/*.ts → data/seed/*.json (일회성, Node 불필요)
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

PostgreSQL 로 전환하려면 `config/settings.py` 의 `DATABASES` 만 바꾸면 됩니다(ORM 만 사용).
