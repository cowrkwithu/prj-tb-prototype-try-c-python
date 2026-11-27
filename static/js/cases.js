/* 대상자 등록조회 — 필터 상태, 상세 탭(URL 반영), 치료 레지멘 차트 */
(function () {
  'use strict';
  window.caseFilters = function (initial) {
    return {
      open: false,
      count: initial,
      recount: function () {
        this.count = this.$root.querySelectorAll('.filter-panel input[type=checkbox]:checked').length;
      },
      reset: function () {
        this.$root.querySelectorAll('.filter-panel input[type=checkbox]').forEach(function (c) { c.checked = false; });
        this.count = 0;
        htmx.trigger(this.$root, 'change');
      },
    };
  };

  // 탭은 화면 안에서 전환하고 주소(?tab=)만 바꾼다 — 새로고침·공유 시 같은 탭이 열린다
  window.caseDetail = function (initialTab) {
    return {
      tab: initialTab,
      init: function () {
        window.caseTab = this.tab;
        var self = this;
        this.$watch('tab', function (v) {
          window.caseTab = v;
          var url = new URL(window.location.href);
          url.searchParams.set('tab', v);
          history.replaceState(history.state, '', url);
          if (v === 0) drawRegimen();
        });
        if (this.tab === 0) self.$nextTick(function () { if (window.Chart) drawRegimen(); else window.addEventListener('load', drawRegimen, { once: true }); });
      },
    };
  };

  var chart = null;
  function drawRegimen() {
    var canvas = document.querySelector('[data-regimen-chart]');
    var dataEl = document.getElementById('regimen-data');
    if (!canvas || !dataEl || !window.Chart) return;
    if (chart) { chart.destroy(); chart = null; }
    var rows = JSON.parse(dataEl.textContent);
    chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: rows.map(function (r) { return r.drug; }),
        // 업로드된 레지멘은 시작일이 0 이 아닐 수 있어 [시작, 종료] 막대(floating bar)로 그린다
        datasets: [{ data: rows.map(function (r) { return [r.start || 0, (r.start || 0) + r.duration]; }),
                     backgroundColor: '#2A7F8E', borderRadius: 4, barThickness: 18 }],
      },
      options: {
        indexAxis: 'y', maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) {
          var r = rows[c.dataIndex], s = r.start || 0;
          return (r.name ? r.name + ' · ' : '') + (s ? s + '–' + (s + r.duration) + ' ' : r.duration + ' ') + canvas.dataset.daysLabel; } } } },
        scales: {
          x: { beginAtZero: true, grid: { color: '#E2E8F0' }, ticks: { color: '#64748B', font: { size: 11 } },
               title: { display: true, text: canvas.dataset.daysLabel, align: 'end', font: { size: 11 } } },
          y: { grid: { color: '#E2E8F0' }, ticks: { color: '#1E293B', font: { size: 12 } } },
        },
      },
    });
  }
})();
