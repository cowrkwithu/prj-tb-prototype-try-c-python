/* 데이터 업로드 화면 보조 스크립트 (Alpine 컴포넌트).
 * 화면 안 즉시 반응(미리보기·행 추가·선택 표시)만 담당한다. 검증·판정은 모두 서버가 한다. */
(function () {
  'use strict';

  function readJson(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent); } catch (e) { return fallback; }
  }
  var seq = 0;
  function uid() { seq += 1; return 'r' + seq; }
  function fmtSize(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(1) + ' GB';
  }

  /* FileDropzone — 끌어다 놓은 파일을 input 에 넣고 이름 목록만 보여 준다 */
  window.dropzone = function (multiple) {
    return {
      drag: false,
      names: [],
      pick: function () { this.show(this.$refs.input.files); },
      drop: function (e) {
        this.drag = false;
        var files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        try {
          var dt = new DataTransfer();
          var n = multiple ? files.length : 1;
          for (var i = 0; i < n; i++) dt.items.add(files[i]);
          this.$refs.input.files = dt.files;
        } catch (err) { return; }
        this.show(this.$refs.input.files);
        this.$refs.input.dispatchEvent(new Event('change', { bubbles: true }));
      },
      show: function (files) {
        var accept = (this.$refs.input.getAttribute('accept') || '').split(',').map(function (s) { return s.trim().toLowerCase(); })
          .filter(function (s) { return s.charAt(0) === '.'; });
        var en = document.documentElement.lang === 'en';
        this.names = Array.prototype.map.call(files || [], function (f) {
          var low = f.name.toLowerCase();
          var ok = !accept.length || accept.some(function (ext) { return low.slice(-ext.length) === ext; });
          return { name: f.name, size: fmtSize(f.size), bad: ok ? '' : (en ? 'File type not allowed' : '허용되지 않는 형식') };
        });
      },
    };
  };

  /* 시퀀스·영상 파일 — 파일 1개 = 요청 1개로 차례로 보낸다(XHR 진행률, 실패한 파일만 재시도).
   * 서버(upload_files)는 요청당 파일 1개만 받고 JSON 으로 결과를 돌려준다. 첫 요청이 임시저장(draft)을 만든다. */
  window.fileUploader = function (cfg) {
    var en = document.documentElement.lang === 'en';
    return {
      id: cfg.id || '', queue: [], busy: false, drag: false, seq: 0,
      pending: function () { return this.queue.some(function (q) { return q.state === 'pending' || q.state === 'failed'; }); },
      add: function (files) {
        var self = this;
        Array.prototype.forEach.call(files || [], function (f) {
          self.seq += 1;
          var over = f.size > cfg.maxBytes;
          self.queue.push({ key: 'f' + self.seq, file: f, name: f.name, size: fmtSize(f.size), pct: 0,
                            state: over ? 'failed' : 'pending', msg: over ? (en ? 'File too large' : '파일이 너무 큽니다') : '' });
        });
      },
      pick: function () { this.add(this.$refs.input.files); this.$refs.input.value = ''; },
      drop: function (e) { this.drag = false; if (e.dataTransfer) this.add(e.dataTransfer.files); },
      drop1: function (q) { this.queue = this.queue.filter(function (x) { return x !== q; }); },
      retry: function (q) { q.state = 'pending'; q.msg = ''; this.start(); },
      start: function () {
        var self = this;
        if (self.busy) return;
        self.busy = true;
        var next = function () {
          var q = self.queue.find(function (x) { return x.state === 'pending'; });
          if (!q) {
            self.busy = false;
            var failed = self.queue.some(function (x) { return x.state === 'failed'; });
            if (!failed && self.id) window.location.href = cfg.url.replace(/\/files$/, '') + '?id=' + encodeURIComponent(self.id) + '&step=1';
            return;
          }
          self.send(q).then(next);
        };
        next();
      },
      send: function (q) {
        var self = this, form = this.$root;
        return new Promise(function (resolve) {
          var fd = new FormData();
          fd.append('csrfmiddlewaretoken', form.querySelector('[name=csrfmiddlewaretoken]').value);
          if (self.id) fd.append('id', self.id); else fd.append('mode', cfg.mode);
          ['condition_id', 'study_date'].forEach(function (n) {
            var el = form.querySelector('[name=' + n + ']');
            if (el) fd.append(n, el.value);
          });
          fd.append('action', 'add');
          fd.append('files', q.file, q.name);
          var xhr = new XMLHttpRequest();
          xhr.open('POST', cfg.url);
          xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
          q.state = 'uploading'; q.pct = 0;
          xhr.upload.onprogress = function (e) { if (e.lengthComputable) q.pct = Math.round(e.loaded / e.total * 100); };
          xhr.onload = function () {
            var res = {};
            try { res = JSON.parse(xhr.responseText); } catch (err) { res = {}; }
            if (res.upload_id && res.upload_id !== self.id) {
              self.id = res.upload_id;  // 첫 파일이 만든 임시저장 — 새로고침해도 이어지도록 주소에 남긴다
              try { history.replaceState(history.state, '', cfg.url.replace(/\/files$/, '') + '?id=' + encodeURIComponent(self.id) + '&step=1'); } catch (err) { /* noop */ }
            }
            if (xhr.status === 200 && res.ok) { q.state = 'done'; q.pct = 100; }
            else { q.state = 'failed'; q.msg = res.message || ((en ? 'Upload failed' : '업로드 실패') + ' (' + xhr.status + ')'); }
            resolve();
          };
          xhr.onerror = function () { q.state = 'failed'; q.msg = en ? 'Network error' : '네트워크 오류'; resolve(); };
          xhr.send(fd);
        });
      },
    };
  };

  /* 임상 폼 — 실시간 미리보기 */
  window.clinicalForm = function () {
    var init = readJson('form-initial', {}) || {};
    var labels = readJson('value-labels', {}) || {};
    var f = {
      condition_id: init.condition_id || '', registration_date: init.registration_date || '', age: init.age || '',
      sex: init.sex || '', country: init.country || '', site: init.site || '', diagnosis_type: init.diagnosis_type || '',
      diagnosis_date: init.diagnosis_date || '', site_of_disease: init.site_of_disease || '',
      prev: init.previous_treatment === 'true', previous_treatment_count: init.previous_treatment_count || '',
      comorbidities: (init.comorbidities || '').split(',').filter(Boolean),
      treatment_outcome: init.treatment_outcome || '', outcome_date: init.outcome_date || '',
    };
    return { f: f, label: function (v) { return labels[v] || v; } };
  };

  /* 레지멘 폼 — 표준 템플릿 적용, 행 추가/삭제, 간트 미리보기 */
  window.regimenForm = function () {
    var init = readJson('regimen-initial', []) || [];
    return {
      templates: readJson('regimen-templates', []),
      codes: readJson('regimen-codes', {}),
      colors: readJson('regimen-colors', {}),
      drugs: init.map(function (d) {
        return { id: uid(), name: d.drug_name || '', code: d.drug_code || '', startDay: d.start_day || '0', endDay: d.end_day || '',
                 dosage: d.dosage || '', route: d.route || 'Oral' };
      }),
      add: function () { this.drugs.push({ id: uid(), name: '', code: '', startDay: '0', endDay: '', dosage: '', route: 'Oral' }); },
      remove: function (i) { this.drugs.splice(i, 1); },
      applyTemplate: function (idx) {
        var tpl = this.templates[Number(idx)];
        if (!tpl) return;
        this.drugs = tpl.drugs.map(function (d) {
          return { id: uid(), name: d.name, code: d.code, startDay: String(d.startDay), endDay: String(d.endDay), dosage: '', route: 'Oral' };
        });
      },
      maxDay: function () {
        return Math.max.apply(null, [1].concat(this.drugs.map(function (d) { return Number(d.endDay) || 0; })));
      },
      bars: function () {
        var max = this.maxDay(), colors = this.colors;
        return this.drugs.filter(function (d) { return d.name && d.endDay; }).map(function (d) {
          var s = Number(d.startDay) || 0, e = Number(d.endDay) || 0;
          return { id: d.id, name: d.name, code: d.code, start: s, end: e, left: s / max * 100,
                   width: Math.max(0, e - s) / max * 100, color: colors[d.code] || '#94A3B8' };
        });
      },
    };
  };

  /* 검사실 폼 — 결과 버튼, DST 토글·내성 프로파일, 생화학 행 */
  window.labForm = function () {
    var init = readJson('lab-initial', {}) || {};
    var flags = init.flags || {};
    var en = document.documentElement.lang === 'en';
    return {
      show: { microscopy: !!flags.microscopy, culture: !!flags.culture, genexpert: !!flags.genexpert, dst: !!flags.dst, biochem: !!flags.biochem },
      v: { microscopy_result: init.microscopy_result || '', culture_result: init.culture_result || '',
           genexpert_mtb: init.genexpert_mtb || '', genexpert_rif: init.genexpert_rif || '' },
      dst: readJson('lab-dst', {}) || {},
      units: readJson('lab-units', {}) || {},
      bc: (readJson('lab-biochem', []) || []).map(function (b) { return { id: uid(), test: b.test || '', value: b.value || '', unit: b.unit || '', date: b.date || '' }; }),
      toggleDst: function (drug, val) { this.dst[drug] = this.dst[drug] === val ? '' : val; },
      addBc: function () { this.bc.push({ id: uid(), test: '', value: '', unit: '', date: '' }); },
      resistant: function () { var d = this.dst; return Object.keys(d).filter(function (k) { return d[k] === 'R'; }); },
      classify: function () {
        var d = this.dst, r = function (k) { return d[k] === 'R'; };
        var inh = r('Isoniazid'), rif = r('Rifampicin');
        var fq = ['Levofloxacin', 'Moxifloxacin', 'Ofloxacin'].some(r), inj = ['Amikacin', 'Kanamycin', 'Capreomycin'].some(r);
        if (inh && rif && fq && inj) return 'XDR-TB';
        if (inh && rif && (fq || inj)) return 'Pre-XDR-TB';
        if (inh && rif) return 'MDR-TB';
        if (inh || rif) return en ? 'Mono-resistance' : '단약제 내성';
        return en ? 'Susceptible (DS-TB)' : '감수성 (DS-TB)';
      },
    };
  };

  /* 영상 어노테이션 폼 */
  window.imagingForm = function (activity) {
    return {
      activity: activity || '',
      findings: (readJson('imaging-findings', []) || []).map(function (a) {
        return { id: uid(), finding: a.finding || '', location: a.location || '', severity: a.severity || '', description: a.description || '' };
      }),
      add: function () { this.findings.push({ id: uid(), finding: '', location: '', severity: '', description: '' }); },
    };
  };
})();
