/* 유전자 분석(GAP) — GWAS 맨해튼 플롯. 데이터는 서버 계산(json_script #gwas-data). */
(function () {
  'use strict';
  function draw() {
    var el = document.getElementById('gwas-data');
    var canvas = document.querySelector('canvas[data-gwas-chart]');
    if (!el || !canvas || !window.Chart) return;
    var d = JSON.parse(el.textContent);
    var sig = d.points.filter(function (p) { return p.sig; });
    var rest = d.points.filter(function (p) { return !p.sig; });
    var ymax = Math.max(d.threshold || 0, Math.max.apply(null, d.points.map(function (p) { return p.y; }).concat([1]))) * 1.15;
    new Chart(canvas, {
      type: 'scatter',
      data: {
        datasets: [
          { label: 'n.s.', data: rest, backgroundColor: 'rgba(148,163,184,.6)', pointRadius: 3 },
          { label: 'significant', data: sig, backgroundColor: 'rgba(239,68,68,.85)', pointRadius: 5 },
          { label: 'Bonferroni', type: 'line', data: [{ x: 0, y: d.threshold }, { x: d.xmax, y: d.threshold }],
            borderColor: '#EF4444', borderDash: [5, 5], borderWidth: 1, pointRadius: 0, fill: false },
        ],
      },
      options: {
        maintainAspectRatio: false, animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            filter: function (item) { return item.datasetIndex < 2; },
            callbacks: {
              label: function (c) {
                var p = c.raw;
                var lines = [p.label + ' (' + (p.gene || canvas.dataset.intergenic) + ')',
                  'Position: ' + (p.approx ? '~' : '') + p.pos.toLocaleString() + (p.approx ? ' · ' + canvas.dataset.approx : ''),
                  'p-value: ' + p.p.toExponential(2), '-log10(p): ' + p.y.toFixed(2)];
                return lines;
              },
            },
          },
        },
        scales: {
          x: { type: 'linear', min: 0, max: d.xmax, ticks: { stepSize: 1, color: '#64748B', font: { size: 10 } },
               title: { display: true, text: canvas.dataset.xLabel, font: { size: 10 } }, grid: { color: '#E2E8F0' } },
          y: { min: 0, max: ymax, ticks: { color: '#64748B', font: { size: 10 } },
               title: { display: true, text: '-log10(p-value)', font: { size: 10 } }, grid: { color: '#E2E8F0' } },
        },
      },
    });
  }
  if (document.readyState === 'complete') draw(); else window.addEventListener('load', draw, { once: true });
})();
