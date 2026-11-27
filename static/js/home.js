/* 홈 대시보드 차트 (Chart.js, 데이터는 json_script 로 주입) */
(function () {
  'use strict';
  function init() {
    var el = document.getElementById('home-chart-data');
    if (!el || !window.Chart) return;
    var data = JSON.parse(el.textContent);
    var grid = '#E2E8F0', tick = { color: '#64748B', font: { size: 11 } };
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

    new Chart(document.getElementById('chart-trend'), {
      type: 'line',
      data: {
        labels: data.trend.labels,
        datasets: data.trend.series.map(function (s) {
          return { label: s.label, data: s.data, borderColor: s.color, backgroundColor: s.color,
                   borderWidth: 2, pointRadius: 2, tension: 0.35 };
        }),
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { labels: { font: { size: 11 }, boxWidth: 12 } } },
        scales: { x: { grid: { color: grid }, ticks: tick }, y: { beginAtZero: true, grid: { color: grid }, ticks: tick } },
      },
    });

    var resColors = ['#16A34A', '#EAB308', '#F97316', '#DC2626', '#64748B', '#94A3B8'];
    new Chart(document.getElementById('chart-resistance'), {
      type: 'doughnut',
      data: { labels: data.resistance.labels, datasets: [{ data: data.resistance.data, backgroundColor: resColors, borderWidth: 1 }] },
      options: { maintainAspectRatio: false, cutout: '60%', plugins: { legend: { display: false } } },
    });
    document.querySelectorAll('.dist-legend .swatch').forEach(function (sw) {
      sw.style.background = resColors[+sw.dataset.idx % resColors.length];
    });

    var outColors = ['#16A34A', '#2A7F8E', '#DC2626', '#1E293B', '#EAB308', '#94A3B8'];
    new Chart(document.getElementById('chart-outcome'), {
      type: 'bar',
      data: { labels: data.outcome.labels, datasets: [{ data: data.outcome.data, backgroundColor: outColors, borderRadius: 4, barThickness: 28 }] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: tick }, y: { beginAtZero: true, grid: { color: grid }, ticks: Object.assign({ precision: 0 }, tick) } },
      },
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
