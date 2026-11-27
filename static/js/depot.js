/* 데이터 탐색(DEPOT) 분석 차트 — 데이터는 서버 집계를 json_script(#depot-chart-data)로 받는다.
   HTMX 로 #depot-result 가 바뀔 때마다 이전 차트를 지우고 다시 그린다. */
(function () {
  'use strict';
  var charts = [];
  var grid = '#E2E8F0', tick = { color: '#64748B', font: { size: 10 } };
  var PIE = ['#2A7F8E', '#1B3A5C'];

  function canvas(name) { return document.querySelector('#depot-result canvas[data-chart="' + name + '"]'); }
  function add(name, cfg) {
    var el = canvas(name);
    if (el) charts.push(new Chart(el, cfg));
  }
  function bar(labels, data, colors, opts) {
    opts = opts || {};
    return {
      type: 'bar',
      data: { labels: labels, datasets: [{ data: data, backgroundColor: colors, borderRadius: 4, maxBarThickness: 40 }] },
      options: {
        maintainAspectRatio: false, animation: false, indexAxis: opts.horizontal ? 'y' : 'x',
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: grid }, ticks: Object.assign({ precision: 0 }, tick), beginAtZero: true },
          y: { grid: { color: grid }, ticks: Object.assign({ precision: 0 }, tick), beginAtZero: true },
        },
      },
    };
  }

  function draw() {
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    var el = document.getElementById('depot-chart-data');
    if (!el || !window.Chart) return;
    var d = JSON.parse(el.textContent);
    if (d.sex) {
      var total = d.sex.data[0] + d.sex.data[1];
      add('sex', {
        type: 'doughnut',
        data: { labels: d.sex.labels, datasets: [{ data: d.sex.data, backgroundColor: PIE, borderWidth: 1 }] },
        options: {
          maintainAspectRatio: false, animation: false, cutout: '55%',
          plugins: {
            legend: { position: 'bottom', labels: { font: { size: 11 }, boxWidth: 12,
              generateLabels: function (chart) {
                return chart.data.labels.map(function (l, i) {
                  var v = chart.data.datasets[0].data[i];
                  return { text: l + ' ' + (total ? Math.round(v / total * 100) : 0) + '%', fillStyle: PIE[i], strokeStyle: PIE[i], index: i };
                });
              } } },
          },
        },
      });
    }
    if (d.country) add('country', bar(d.country.labels, d.country.data, '#2A7F8E', { horizontal: true }));
    if (d.diag) add('diag', bar(d.diag.labels, d.diag.data, d.diag.colors));
    if (d.outcome) {
      add('outcome', {
        type: 'bar',
        data: { labels: d.outcome.labels, datasets: d.outcome.series.map(function (s) {
          return { label: s.label, data: s.data, backgroundColor: s.color, stack: 'a', maxBarThickness: 48 };
        }) },
        options: {
          maintainAspectRatio: false, animation: false,
          plugins: { legend: { labels: { font: { size: 10 }, boxWidth: 10 } } },
          scales: { x: { stacked: true, grid: { color: grid }, ticks: tick },
                    y: { stacked: true, beginAtZero: true, grid: { color: grid }, ticks: Object.assign({ precision: 0 }, tick) } },
        },
      });
    }
    if (d.lineage) add('lineage', bar(d.lineage.labels, d.lineage.data, '#1B3A5C'));
    if (d.findings) add('findings', bar(d.findings.labels, d.findings.data, d.findings.colors));
  }

  function init() {
    if (window.Chart) Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    draw();
  }
  if (document.readyState === 'complete') init(); else window.addEventListener('load', init);
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (e.target && e.target.id === 'depot-result') draw();
  });
})();
