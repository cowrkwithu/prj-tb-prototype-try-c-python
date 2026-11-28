# 구현 계획 — TB Research Portal Python Edition

원본: `/home/john/gitRepositories/prj-tb-prototype-try-b` (README.md, src/App.tsx 라우트 참고)
진행 방식: 헤더(계획·판단) → 빌더(구현) → 적대적 리뷰어(검증) → 빌더(수정) → 헤더(합격 판정)

## 단계

| 단계 | 범위 | 원본 대응 |
|---|---|---|
| 0 기반 | venv·setup/run 스크립트(sh/bat), Django 골격, accounts(로그인/로그아웃/프로필, 역할), base 레이아웃(헤더·푸터·사이드바·햄버거·토스트·언어토글), 정적 자산 동봉, 시드: 데이터사전 엑셀 v3.2.0 → DB, 환자 Mock → DB, `bootstrap` 커맨드 | contexts/*, components/layout/*, pages/Auth/*, data/patients.ts, scripts/gen_dict.py |
| 1 핵심 화면 | 홈 대시보드(통계 DB 집계), 케이스 에디터(목록 검색/필터 + 상세 6탭), 표준 데이터사전 뷰어(카드/테이블, 타입 필터, 검색, 필드 상세) | pages/Home, pages/CaseEditor, pages/Dictionary |
| 2 업로드·검증 | 업로드 허브 + 임상/레지멘/검사실/유전체/영상 5종(폼·CSV, 유효성검사, 미리보기, 비식별화 점검, 임시저장), 관리자 대시보드·검증 대기큐 승인/반려, 내 업로드 | pages/Upload/*, components/upload/*, utils/*, pages/Admin/Dashboard, ReviewDashboard |
| 3 코호트·스키마·공유 | DEPOT 코호트 빌더(AND/OR)·분석 5탭·저장 코호트, 스키마 탐색/생성/상세/데이터 입력, 관리자 데이터사전·스키마 관리, 데이터 공유(카탈로그·API 문서·DAR·내보내기) | pages/Depot, pages/Schemas, components/schema, pages/Admin/DictionaryManager, SchemaManager, pages/DataSharing |
| 4 GAP·RAP | 유전체 브라우저·약제내성 예측·GWAS·유사도·변이 검색, 영상 뷰어·타임시리즈·어노테이션 | pages/Gap/*, pages/Rap/* |
| 5 마무리 | 사용자 매뉴얼(아코디언·검색·한/영), README, 이식성 검증(깨끗한 venv 재설치, Windows 스크립트 점검), 전체 회귀 리뷰 | pages/Manual, README |

## 단계 완료 기준
- `pytest` 전체 통과, `python manage.py check` 무경고
- 깨끗한 디렉터리에서 `setup.sh` → `run.sh`로 기동 가능
- 리뷰어의 CONFIRMED 결함 0건(또는 헤더가 사유를 기록하고 보류)

## 진행 기록
(각 단계 종료 시 헤더가 기록)

### 0단계 — 2026-11-26 조건부 합격
- 1차 리뷰 FAIL(CONFIRMED 2: dev 모드 0.0.0.0+DEBUG 노출, TS 변환기 무증상 오변환 / PLAUSIBLE 3: PG 시퀀스, 삭제 시드 부활, 로그인 sensitive_post_parameters) → 전부 수정.
- 2차 리뷰 FAIL(CONFIRMED 1: 변환기 엄격화로 최상위 스칼라 export 거부) → 현재 시드 영향 없음, 1단계와 함께 수정·재검증.
- 헤더 결정: 배포 전이므로 dictionary 0001 재작성 허용, 이후 마이그레이션 재작성 금지(CLAUDE.md 반영).
- 결과: pytest 107 통과, 데이터사전 19/1,257·환자 50 원본과 전수 일치.

### 1단계 — 2026-11-26 합격 (리뷰 PASS, CONFIRMED 0)
- 홈(DB 집계), 케이스 에디터(원본 실제 7탭, 읽기 전용, URL 상태), 데이터사전(원본 검색 알고리즘 이식, 규칙 기반 AI 도우미).
- 0단계 잔여 CONFIRMED(변환기 스칼라 export) 해결 확인. pytest 187 통과.
- 헤더 결정: 월별 추이는 최근 등록월 기준 유지, 홈 편집 콘텐츠는 2단계에서 DB 모델화.
- NIT 5건(영상 썸네일 상한, 기준기간 표시, 툴팁 누락, 도우미 sendError, 정렬 방식) → 2단계에서 처리(정렬 차이는 수용).

### 2단계 — 2026-11-26 합격 (3차 리뷰 PASS)
- 업로드 5종(폼·CSV·파일) → 서버 검증 → 비식별화(PII 스캔+7항목) → pending → 관리자 승인 시 트랜잭션으로 도메인 반영. 서버 draft, 내 업로드, 관리자 대시보드·검토, 홈 콘텐츠 DB화.
- 1차 FAIL(CONFIRMED 7: warning 값 반영, 갱신 시 값 삭제, DICOM 4MB 우회, CSV 수식 주입, PII 변형 우회, 재승인 중복, upload_id 1000) → 수정.
- 2차 FAIL(CONFIRMED 3: DICOM 중첩 재귀, 요소 상한, waitress 4GB 버퍼) → 수정.
- 헤더 결정: 판독 불가 DICOM 차단, 임상 갱신 빈 칸=유지, 관리자 본인 업로드 승인 금지, 파일 업로드는 요청당 1개, waitress 상한=단일 파일 상한+16MB(남는 위험은 내부망 전제로 수용, README에 리버스 프록시 권고).
- 이월(3c): 같은 제출 내 동일 sha256 파일 중복, 삭제 요청의 관리자 처리 화면. pytest 312 통과.

### 3단계 분할 (헤더 결정)
- 3a 스키마·관리자 데이터사전/스키마 관리 ∥ 3b DEPOT 코호트 (병렬, 앱 분리) → 3c 데이터 공유(3a·3b 의존).

### 3b DEPOT — 2026-11-26 합격 (5차 리뷰 PASS)
- 조건 트리 → ORM(허용 목록 17필드, 깊이5/노드100/값500/8,000자 상한), 분석 5탭(실제 Fisher·Welch), 저장 코호트(조건 복원·스냅샷 비교, 소유자만 수정).
- 리뷰 경과: SQLite 변수 한도(Windows 32,766) → JSON 커스텀 Lookup(has_any_value), loaded=² 500, Fisher n³ → lgamma, 긴 조건 저장 리다이렉트, 폼 경로 ConditionError → 단일 진입점 resolve_conditions, 고립 서로게이트 → 전역 safejson + 미들웨어.
- 퍼징 1,264요청 500 0건. PostgreSQL 경로는 코드 검토만(실행 검증은 5단계에서 사용자 승인 시).

### 3a 스키마·관리자 데이터사전/스키마 관리 — 2026-11-27 합격 (6차 리뷰 PASS)
- 스키마 탐색/생성(빌더)/상세/데이터 입력(폼·CSV, 검사→검토→제출), 가져오기·내보내기(JSON/CSV), 소유자·관리자 상태 전이, 관리자 데이터사전 관리.
- 1·2차: 항목 수정 모달 무변경 저장, 위험 정규식(시간 예산·패턴별 timeout·탐침), 정수 파싱, structure_rev 분리.
- 3차 FAIL(CONFIRMED 5): 정규식 컴파일 메모리 고갈, 정의 JSON 타입 혼동 500, 사전 `_num` 무한대 500, 필드명 `__`로 고유값 검사 무력화, 불안정 테스트 → 수정.
- 4차 FAIL(CONFIRMED 3): 손 파서 정적 검사 우회 4종, published 구조 draft 저장 우회, 사전 필드명 규칙 누락 → 수정.
- 5차 FAIL(CONFIRMED 1): regex 모듈 내부 캐시(500)가 앱 LRU 우회 → `cache_pattern=False`, 정의당 고유 패턴 50, 예산 소진 시 컴파일 생략.
- 헤더 결정:
  - 사용자 정규식은 **허용 목록 문법**(apps/schemas/user_regex.py)만 지원 — 인라인 플래그·역참조·`\n`·`{,m}`·possessive 등 불가(수용). 크기 상한: 반복 1000, 중첩 곱 10,000, 펼친 크기 10,000.
  - 탐침 길이는 필드 타입 최대 입력 길이 기준(TEXT 1000/TEXTAREA 10000) — 같은 패턴이 TEXT는 통과, TEXTAREA는 거부될 수 있음.
  - 앱 컴파일 캐시 LRU 64(최대 약 130MB 상주) 수용.
  - 필드명 규칙은 apps/core/naming.py 한 곳(`__`·끝 밑줄 금지, 64자) — 기존 이름은 legacy 허용.
  - 결과 상태가 published 인 모든 전이(publish/restore)와 published 구조 저장은 strict 검증.
  - 보관 상태이면서 strict 실패인 구조는 복원 불가(관리자 삭제로 처리) 수용.
- NIT 이월(3c): 가져오기 확정 시 정의 검사 2회(CheckContext 재사용).
- 미검증: PostgreSQL 실행(5단계). pytest 1054 통과.

### 3c 데이터 공유 — 범위 결정 (헤더)
- 원본 하드코딩 건수 → DB 집계. DAR 자동 승인 시뮬레이션 → DB 저장 + 관리자 승인/반려(사용자×데이터 유형).
- 다운로드·내보내기는 서버에서 DAR 승인 확인. API 키는 승인 후 발급(해시 저장, 1회 표시).
- API: GET cases, cases/{id}, images/{id}(메타·파일), POST cohorts 실제 구현. genomics/variants 는 변이 모델이 생기는 4단계로 이월(그때까지 JSON "준비 중" 응답).
- 2단계 이월분: 동일 제출 내 sha256 중복 파일, 삭제 요청 관리자 처리 화면.

### 3c 데이터 공유 — 2026-11-27 합격 (4차 리뷰 PASS)
- /data 4탭(데이터셋·API·DAR·내 업로드), DAR DB 저장+관리자 승인/반려(유형별 부분 승인, 본인 처리 금지), 승인 강제 CSV/엑셀 내보내기(수식 주입 방지), 실제 API(cases, cases/{id}, images/{id}, POST cohorts; 키 해시 저장·1회 표시, 키당·인증 실패 IP당 요청 제한), 같은 제출 내 SHA-256 중복 거부, 업로드 삭제 요청 관리자 처리.
- 1차 FAIL(CONFIRMED 4): API children 스칼라 500, 삭제 시 시드 유전체 값 파괴, CXR 소견 잔존, 중복 건너뛴 업로드의 데이터 삭제 → 증분 역연산으로 수정.
- 2차 FAIL(CONFIRMED 4): 역연산이 체인·공유에서 어긋남 → 기준 스냅샷 + fold 재계산으로 재설계.
- 3차 FAIL(CONFIRMED 2): 다중 파일 부분 공유, 예전 건 섞인 환자 → 삭제를 **환자×도메인 재적용(replay)** 으로 재설계(공유 기록 의존 제거).
- 헤더 결정:
  - 업로드 삭제 승인 = 영향 (환자, 도메인)의 업로드 소유 행 삭제 → 기준 스냅샷 복원 → 남은 승인 업로드를 approval_seq 순으로 최초 승인과 같은 코드로 재적용. 불변식 "전부 승인 후 일부 삭제 == 남은 것만 승인"을 하네스 테스트로 상시 검증.
  - Submission.approval_seq(승인 순서, 시계 무관). 승인·삭제 모두 환자 id 오름차순 잠금 → 업로드.
  - 3c 이전 승인 건이 섞인 (환자, 도메인)은 재적용하지 않고 화면에 "예전 건 — 되돌리지 않음" 안내.
  - 임상 덮어쓰기·DST/도말/배양 병합·last_update_date 는 복원 불가(안내). 공유 행은 재적용 시 새 pk.
  - DAR 시드 없음, DAR 현황은 본인 신청만. 유전체 카드 건수=SequenceRun, 변이 API 는 4단계까지 503.
  - 요청 제한은 프로세스 메모리 캐시(다중 프로세스·프록시 뒤 IP 공유 한계).
- 5단계 이월:
  - [성능/가용성] 삭제 재적용 비용이 환자에 반영된 전체 업로드에 비례(1000행×8 업로드에서 삭제 19초). 그동안 다른 쓰기 요청이 SQLite busy_timeout(5초) 초과로 500 가능 → 재적용 시 대상 환자 행만 정제(clean_rows 범위 축소) + 쓰기 뷰 공통 DatabaseError 처리(또는 운영 한계 문서화).
  - NIT: 임상 업로드 삭제 시 참조 검사가 승인 때 건너뛴 행도 셈(보수적 유지). /data 유전체 "최종 업데이트"가 재적용으로 삭제일로 바뀜.
  - PostgreSQL 실행 검증(잠금 순서·approval_seq 재시도는 코드 검토만).
- pytest 1536 통과.

## 4단계 GAP·RAP — 범위 결정 (헤더)
원칙: 원본의 mock 수치(mockResults, generateMockSnps, strainPoint, generateMockImages)는 쓰지 않고 DB에서 계산한다. 계산할 근거가 없는 것은 이름을 정직하게 바꾸거나 "참고용" 표시.
- **genomics 앱(신규)**: ResistanceGene(10, 시드), Variant 카탈로그(원본 20건 시드: 유전자·위치·ref/alt·아미노산 변화·약제·주석). sampleCount/frequency 는 시드 숫자가 아니라 환자 resistance_mutations(및 SequenceRun 보유) 에서 집계. 환자 변이 문자열 ↔ 카탈로그 매칭 규칙을 한 곳(services)에 둔다.
- **유전체 브라우저(/gap)**: 유전자 트랙(H37Rv 좌표 GENE_SPANS) + DB 변이, 유전자 선택·확대, 변이 클릭 시 보유 환자 목록(케이스 상세 링크).
- **변이 검색(/gap/search)**: 유전자·약제·아미노산·위치 범위 필터, 페이지네이션, CSV 내보내기는 3c DAR(유전체) 승인 게이트 재사용.
- **약제내성 예측(/gap/resistance)**: 입력 = 시퀀스 보유 환자 선택 또는 변이 목록 업로드(VCF/CSV/TSV 텍스트, 크기·행수 제한). 카탈로그 규칙 엔진: 카탈로그 변이 검출 → R, 해당 약제 유전자 검사됨·무변이 → S, 근거 없음 → U. 알고리즘 3종은 실제 외부 도구가 아니므로 "카탈로그 기반 규칙" 임을 화면에 명시(원본 이름 유지 시 규칙 차이를 실제로 구현: 예 TB-Profiler=전체 카탈로그, Mykrobe=고빈도 변이만, ResFinder=획득 유전자 없음→대부분 U 등, 헤더는 정직성만 요구). 실행 결과 PredictionRun 으로 DB 저장·이력 조회(본인 것만, 관리자 전체).
- **GWAS(/gap/gwas)**: 환자 변이 × DST 표현형(R vs S)에 대한 Fisher 정확검정(순수 파이썬, scipy 금지) → −log10 p 맨해튼 플롯(Chart.js scatter), Bonferroni 기준선, 약제 선택, 표본 수 부족 시 안내.
- **유사도(/gap/similarity)**: 선택 환자(최대 50, 코호트에서 불러오기 가능) 간 거리 행렬을 변이 집합으로 계산. 방법: SNP 거리(대칭차 수), Jaccard 거리, 계통 가중(다른 lineage 면 가산) — cgMLST/Phylogenetic 이라는 이름은 쓰지 않는다. 대칭·대각 0 보장, 히트맵.
- **API**: /api/v1/genomics/variants 503 → 실제 구현(필터·페이지네이션, 3c 키·레이트리밋 그대로).
- **RAP(/rap, /rap/timeseries, /rap/annotation)**: 데이터는 ImagingStudy(DB). 시드: 영상 보유 시드 환자에게 static/imaging 공개 자산(CXR 5장 + CT/PET-CT 볼륨)을 가리키는 ImagingStudy 를 멱등 생성하고 "공개 샘플 영상(출처·결핵 아님 표시)" 플래그. 업로드된 영상은 기존 SubmissionFile 을 권한 검사 뷰로 서빙.
  - 뷰어: 확대·이동·밝기·대비, CXR 판독 ROI(rapImaging.json → DB) 오버레이 토글, CT/PET-CT 평면(axial/coronal/mip) 단면 슬라이더.
  - 타임시리즈: 환자 검사들을 촬영일순으로 재생/일시정지/정지, 소견 변화 비교.
  - 전문가 소견(어노테이션): 원본 localStorage → DB 모델(검사·영상/평면/단면·도형 rect/ellipse/text·% 좌표·소견 유형·메모·작성자). 생성/수정/삭제는 POST+CSRF, 작성자 본인 또는 admin 만 수정·삭제, 좌표 범위·필드 서버 검증.
- 모든 새 뷰 퍼징(500 금지), 권한 테스트, 이중 언어 문구.

### 4단계 합격 (2차 리뷰 PASS, 2026-11-27) — pytest 1696 통과
- 1차 FAIL(확정 3): 연관 유전자에 다른 약제 카탈로그 변이가 있어도 S 판정 / 케이스 상세 업로드 목록에 공개 샘플 혼입 / API 가 공개 샘플을 실제 검사처럼 노출. 미확정 1: 열람 불가 업로드 영상의 전문가 소견 노출.
- 헤더 결정: Variant.drugs 다약제 연관(genomics 0002, rrs a1401g→AMK·KAN·CAP, inhA c-15t→INH·ETH, eis c-14t→KAN) + 불변식 "연관 유전자 변이 검출 시 S 금지"(전 조합 테스트). 공개 샘플은 외부 공유 경로(API·내보내기) 전부 제외. 열람 불가 검사 소견은 개수만. 케이스 모달 소견은 seed_key 로 검사 단위 연결. /data 유전체 내보내기 변이 단위 그룹·분모를 GAP 과 통일.
- 범위 외 소폭 변경(수용): /data 유전체 좌표 채움, 케이스 영상 모달 DB 소견 표시, 업로드 영상 원 파일명은 열람 권한자만.
- 5단계 이월: 케이스 에디터 영상 탭을 DB 검사 기반으로 통합(현재 generate_images 파생 목록), DICOM 미리보기 없음, 변이 집계 캐시 없음(요청마다 전 환자 스캔), 업로드 영상 메타데이터(촬영일·소견) 로그인 사용자 전체 공개 정책 재검토, NIT: 영상 수만 수동 변경 시 force 재시드가 샘플 추가, fabG1 카탈로그 편입 검토, 소견 500건 상한 동시성. PostgreSQL 실행 미검증. 수정 후 브라우저 수동 확인 미실시.

## 5단계 마무리 — 범위 결정 (헤더)
- **사용자 매뉴얼(/manual)**: 원본 pages/Manual(manualContent.ts, ManualPage.tsx) 계승 — 아코디언·검색(한/영)·목차·스크린샷(static/manual). 내용은 DB 모델 + 멱등 시드(manual 앱), 화면은 DB에서 읽음. 원본 문구 중 Python 판과 동작이 다른 부분(예: 약제내성 예측은 카탈로그 규칙, 유사도 방법명, DAR 관리자 승인, 서버 저장)은 실제 동작에 맞게 고친다.
- **README**: Windows/Linux 설치·실행(setup/run 스크립트, 수동 명령), 데모 계정, 표준 명령, 구조, 설정 환경변수(TB_PORT/TB_BIND/TB_SECRET_KEY 등), PostgreSQL 전환법, 알려진 한계(아래 이월 중 수용한 것).
- **이식성 검증**: 저장소를 scratchpad 로 git clone(깨끗한 디렉터리) → setup.sh → bootstrap → run.sh 다른 포트 기동·스모크(로그인·주요 화면 200) → 종료. .bat 정적 점검(경로·인코딩·chcp·venv 경로). 코드 전체 grep: open() encoding 누락, os.path 셸 의존, 하드코딩 '/' 경로.
- **이월 처리**:
  - 삭제 재적용 성능: clean_rows 등 재적용 범위를 대상 환자로 축소, 1000행×8 업로드 삭제 시간 측정 전후 보고. 쓰기 중 DB 잠금(OperationalError locked/busy)은 공통 처리로 500 대신 503 안내 화면(HTMX/JSON 요청은 해당 형식).
  - 케이스 에디터 영상 탭을 DB ImagingStudy(공개 샘플 표시 포함 + 업로드) 기반으로 통합, RAP 과 같은 목록·소견.
  - 수정 후 브라우저 확인 미실시 화면(RAP 숨김 문구, GWAS 연관 약제 열) 헤드리스 확인.
  - 수용(README 한계로 기록): 변이 집계 캐시 없음, 업로드 영상 메타데이터 로그인 사용자 공개, force 재시드 NIT, fabG1 미편입, 소견 상한 동시성, 요청 제한 프로세스 메모리, DICOM 미리보기 없음.
- **PostgreSQL 실행 검증**: 로컬 공용 PG 서버 사용은 사용자 승인 필요 — 별도 진행.
- 끝으로 전체 회귀 리뷰(reviewer).
- (갱신) PostgreSQL: 사용자 승인으로 전용 상시 컨테이너 tbportal-pg(postgres:16, 127.0.0.1:5462, 볼륨 tbportal-pgdata) 사용. var/.env 로더(config/envfile.py, 기존 환경변수 우선, 빈 값이면 SQLite) 추가, var/.env 활성 → 개발 기본 DB 는 PG. PG pytest 1차 10 실패(NUL 500 6건 = 코드 결함 수정, SQLite 가정 테스트 4건 분기) → 1799 통과. SQLite 1796 통과. 원인 미확인 간헐 실패 1회(재현 안 됨).

### 5단계 합격 (4차 리뷰 PASS, 2026-11-28) — SQLite 1826 통과/8 skip, PostgreSQL 16 1832 통과/2 skip
- 구현: 사용자 매뉴얼(manual 앱, DB 시드, 원본 문구 40건 실제 동작에 맞게 교정), README, 이식성(깨끗한 venv setup→bootstrap→run·prod 기동), 삭제 재적용 성능(wide 11.5s→1.5s), DB 잠금 503 미들웨어, 케이스 영상 탭 DB 통합, var/.env 로더, PostgreSQL 실행 검증(전용 컨테이너 tbportal-pg).
- 리뷰 경과: 1차 FAIL(확정 4: PG NUL 500 22경로 / 검토 사유 NUL / 503 "저장 안 됨"인데 일부 커밋 / DEBUG 화면 DB 비밀번호) → NUL 퍼징을 URL resolver 전 패턴 × GET/POST 하네스로, 자유 텍스트 공통 검사, 쓰기 뷰 atomic_write(감사 표), 예외 보고 필터. 2차 FAIL(확정 1: 커밋 후 콜백 실패 시 새 파일 삭제) → 커밋 표시·robust 지연 삭제. 3차 FAIL(확정 1: 불변식 테스트 자정 직후 실패 — 1차의 원인 미확인 간헐 실패) → 테스트 시계 정오 고정(헤더 직접 수정). 4차 PASS.
- 수용 NIT: 몇 초 창의 날짜 경계 의존 테스트 4건, 바깥 atomic 롤백 시 고아 파일(cleanup_media 회수), 느린 디스크 2GB 업로드 시 SQLite 잠금 5초 초과 가능(기존 한계).
