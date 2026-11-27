# static/vendor — 동봉 JS 라이브러리

CDN 을 쓰지 않고 아래 파일을 그대로 서빙한다(오프라인 동작). 갱신 시 버전·출처·SHA-256 을 함께 고친다.

| 파일 | 라이브러리 | 버전 | 출처 | SHA-256 |
|---|---|---|---|---|
| htmx.min.js | htmx | 2.0.4 | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447 |
| alpine.min.js | Alpine.js | 3.14.8 | https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js | b600e363d99d95444db54acbfb2deffec9ae792aa99a09229bcda078e5b55643 |
| chart.umd.min.js | Chart.js | 4.4.7 | https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js | 42f4dbfe70c430ffc22c7128f36cae8d599cac90b8cd37c8f42a59e565e5deb7 (수정본) |

아이콘: `static/icons/*.svg` — lucide-static 0.460.0 (ISC), https://unpkg.com/lucide-static@0.460.0/icons/<name>.svg
에서 필요한 것만 받아 둠. 템플릿에서는 `{% icon "name" "class" %}` 로 인라인한다.

## 원본 대비 수정

- `chart.umd.min.js`: 마지막 줄의 sourcemap 주석(`//# sourceMappingURL=chart.umd.js.map`)을 제거했다.
  .map 파일을 동봉하지 않으므로 브라우저 개발자도구의 404 요청을 막기 위함이다.
  원본 배포 파일 SHA-256: `206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e`
  → 제거 후: `42f4dbfe70c430ffc22c7128f36cae8d599cac90b8cd37c8f42a59e565e5deb7`
- htmx.min.js · alpine.min.js 는 배포본 그대로(sourcemap 주석 없음).
