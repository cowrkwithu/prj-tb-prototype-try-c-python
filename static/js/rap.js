/* 영상 분석(RAP) 뷰어 — 원본 pages/Rap/{index,ImageViewer,VolumeViewer,ExpertOverlay}.tsx 동작.
   데이터는 서버(json_script #rap-data, DB). 전문가 소견 생성/수정/삭제는 POST + CSRF(fetch) — 권한은 서버가 검사한다. */
(function () {
  'use strict';
  var TABS = ['/rap', '/rap/timeseries', '/rap/annotation'];

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }
  function clamp(v) { return Math.min(100, Math.max(0, v)); }
  function pad3(n) { return String(n).padStart(3, '0'); }

  window.rapViewer = function () {
    var data = JSON.parse(document.getElementById('rap-data').textContent);
    return {
      data: data, en: data.en, studies: data.studies, annotations: data.annotations,
      sel: Math.min(data.selected || 0, Math.max(0, data.studies.length - 1)), tab: data.tab,
      zoom: 1, bright: 100, contrast: 100, panX: 0, panY: 0, pan: null,
      showAI: true, showExpert: true, tool: 'select', drag: null, draft: null, editingId: null, selectedId: null,
      form: { finding_type: '', location: '', severity: '', text: '' }, formError: '', busy: false,
      plane: 'axial', idx: { axial: 0, coronal: 0, mip: 0 }, cine: false, cineTimer: null,
      playState: 'stopped', playTimer: null, compare: false, cmpA: 0, cmpB: Math.min(1, data.studies.length - 1),

      init: function () {
        var self = this;
        this.resetVolume();
        // 썸네일 선택·재생으로 영상이 바뀌면 타임라인의 그 항목이 보이도록 패널을 굴린다(원본 TimeseriesTab)
        this.$watch('sel', function () {
          if (self.tab !== 1 || self.compare) return;
          self.$nextTick(function () { var el = document.querySelector('.rt-card.on'); if (el) el.scrollIntoView({ block: 'nearest' }); });
        });
      },
      get cur() { return this.studies[this.sel] || this.studies[0]; },
      get formGeom() {
        if (this.editingId) return this.annotations.find(function (a) { return a.id === this.editingId; }, this) || null;
        return this.draft;
      },

      /* ─── 탭 · 영상 선택 ─── */
      setTab: function (i, href) {
        this.tab = i;
        var url = new URL(window.location.href);
        url.pathname = href;
        url.searchParams.set('study', this.cur.id);
        history.pushState(history.state, '', url);
        if (i !== 1 && this.playState === 'playing') this.pause();
      },
      selectStudy: function (i) {
        if (i === this.sel) return;
        this.clearEdit(); this.selectedId = null;
        this.sel = i;
        this.resetVolume();
        var url = new URL(window.location.href);
        url.searchParams.set('study', this.cur.id);
        history.replaceState(history.state, '', url);
      },
      resetVolume: function () {
        this.stopCine();
        var v = this.cur && this.cur.volume;
        this.plane = 'axial';
        if (v) this.idx = { axial: v.initial.axial, coronal: v.initial.coronal, mip: 0 };
      },

      /* ─── 보기 조절 ─── */
      zoomBy: function (d) { this.zoom = Math.round(Math.min(3, Math.max(0.5, this.zoom + d)) * 10) / 10; if (this.zoom <= 1) { this.panX = 0; this.panY = 0; } },
      resetView: function () { this.zoom = 1; this.bright = 100; this.contrast = 100; this.panX = 0; this.panY = 0; },
      frameStyle: function () { return 'transform: translate(' + this.panX + 'px, ' + this.panY + 'px) scale(' + this.zoom + ')'; },

      /* ─── 볼륨 ─── */
      planeLabel: function (p) { return p === 'axial' ? (this.en ? 'Axial' : '축상') : p === 'coronal' ? (this.en ? 'Coronal' : '관상') : 'MIP'; },
      count: function () { var v = this.cur.volume; return v ? (v.counts[this.plane] || 1) : 1; },
      setPlane: function (p) { this.plane = p; this.stopCine(); },
      setSlice: function (n) { this.stopCine(); this.idx[this.plane] = Math.min(this.count() - 1, Math.max(0, n)); },
      step: function (d, wrap) {
        var n = this.count(), next = this.idx[this.plane] + d;
        this.idx[this.plane] = wrap ? (next + n) % n : Math.min(n - 1, Math.max(0, next));
      },
      toggleCine: function () {
        if (this.cine) { this.stopCine(); return; }
        var self = this;
        this.cine = true;
        this.cineTimer = setInterval(function () { self.step(1, true); }, this.plane === 'mip' ? 120 : 80);
      },
      stopCine: function () { this.cine = false; if (this.cineTimer) { clearInterval(this.cineTimer); this.cineTimer = null; } },
      onWheel: function (e) {
        if (this.cur.kind !== 'volume') return;
        e.preventDefault();
        this.stopCine();
        this.step(e.deltaY > 0 ? 1 : -1, false);
      },
      imageSrc: function () {
        var c = this.cur;
        if (c.kind === 'volume') return c.volume.dir + this.plane + '/' + pad3(this.idx[this.plane]) + '.jpg';
        return c.image || '';
      },
      lesionBox: function () {
        var v = this.cur.volume;
        if (!v) return null;
        var i = this.idx[this.plane];
        if (this.plane === 'axial') return i >= v.slice_range[0] && i <= v.slice_range[1] ? v.axial_box : null;
        if (this.plane === 'coronal') return v.coronal_rows.indexOf(i) >= 0 ? v.coronal_box : null;
        return null;
      },
      hasAI: function () { return this.cur.kind === 'cxr' ? this.cur.rois.length > 0 : !!this.lesionBox(); },
      aiCount: function () { return this.cur.kind === 'cxr' ? this.cur.rois.length : (this.cur.kind === 'volume' ? 1 : 0); },
      caption1: function () {
        var c = this.cur;
        if (c.kind === 'volume') return c.type + ' · ' + this.planeLabel(this.plane) + ' ' + (this.idx[this.plane] + 1) + '/' + this.count();
        return c.type === 'CXR' ? 'CXR · PA' : c.type;
      },
      caption2: function () {
        var c = this.cur, v = c.volume;
        if (c.kind !== 'volume') return c.date;
        if (this.plane === 'axial') {
          var z = v.z_range[0] + (v.z_range[1] - v.z_range[0]) * (this.idx.axial / Math.max(1, v.counts.axial - 1));
          return 'z ' + z.toFixed(1) + ' mm · ' + v.thickness + ' mm';
        }
        if (this.plane === 'mip') return Math.round(this.idx.mip / v.counts.mip * 360) + '°';
        return c.date;
      },
      caption3: function () { return this.cur.kind === 'volume' && this.cur.type === 'PET-CT' && this.plane !== 'mip' ? '18F-FDG · SUV 0–8' : ''; },
      boxStyle: function (r, color) {
        return 'left:' + r.x + '%;top:' + r.y + '%;width:' + r.width + '%;height:' + r.height + '%;border-color:' + color + ';background:' + color + '20';
      },
      markers: function () {
        var id = this.cur.id, plane = this.plane, seen = {};
        this.annotations.forEach(function (a) { if (a.study === id && a.plane === plane && a.slice !== null) seen[a.slice] = 1; });
        return Object.keys(seen).map(Number);
      },

      /* ─── 전문가 소견 표시 ─── */
      curExpert: function () { var id = this.cur.id; return this.annotations.filter(function (a) { return a.study === id; }); },
      expertOf: function (s) { if (!s) return []; return this.annotations.filter(function (a) { return a.study === s.id; }); },
      visibleExpert: function () {
        var c = this.cur, plane = this.plane, i = this.idx[this.plane];
        return this.annotations.filter(function (a) {
          if (a.study !== c.id) return false;
          if (c.kind !== 'volume') return a.plane === null;
          return a.plane === plane && a.slice === i;
        });
      },
      markClass: function (g) {
        var cls = g.shape === 'text' ? 'xm xm-textwrap' : 'xm ' + (g.shape === 'ellipse' ? 'xm-ellipse' : 'xm-rect');
        if (g.id && g.id === this.selectedId) cls += ' on';
        if (this.tool === 'select' && g.id) cls += ' clickable';
        return cls;
      },
      markStyle: function (g) {
        if (g.shape === 'text') return 'left:' + g.x + '%;top:' + g.y + '%';
        return 'left:' + g.x + '%;top:' + g.y + '%;width:' + g.width + '%;height:' + g.height + '%';
      },
      shapeLabel: function (s) { return s === 'rect' ? (this.en ? 'Rectangle' : '네모') : s === 'ellipse' ? (this.en ? 'Circle' : '동그라미') : (this.en ? 'Text' : '텍스트'); },
      sliceLabel: function (g) {
        if (!g || !g.plane || g.slice === null || g.slice === undefined) return '';
        var s = this.studies.find(function (x) { return x.id === (g.study || this.cur.id); }, this) || this.cur;
        var n = s.volume ? s.volume.counts[g.plane] : '';
        return this.planeLabel(g.plane) + ' ' + (g.slice + 1) + '/' + n;
      },
      toggleSelect: function (id) { this.selectedId = this.selectedId === id ? null : id; },
      locate: function (a) {
        this.showExpert = true;
        this.selectedId = a.id;
        if (a.plane && this.cur.kind === 'volume') { this.stopCine(); this.plane = a.plane; this.idx[a.plane] = a.slice; }
      },

      /* ─── 그리기 · 이동(팬) ─── */
      setTool: function (t) { this.tool = t; if (t === 'select') this.draft = null; },
      toPct: function (e) {
        var r = this.$refs.overlay.getBoundingClientRect();
        return { x: clamp((e.clientX - r.left) / r.width * 100), y: clamp((e.clientY - r.top) / r.height * 100) };
      },
      stageDown: function (e) {
        if (e.button !== 0) return;
        if (this.tool === 'select') {
          if (this.zoom > 1 && (this.cur.image || this.cur.kind === 'volume')) {
            this.pan = { x0: e.clientX, y0: e.clientY, px: this.panX, py: this.panY };
            e.currentTarget.setPointerCapture(e.pointerId);
          }
          return;
        }
        if (!this.cur.annotatable || !this.$refs.overlay) return;
        var r = this.$refs.overlay.getBoundingClientRect();
        if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) return;
        e.preventDefault();
        var p = this.toPct(e);
        if (this.tool === 'text') { this.startDraft({ shape: 'text', x: p.x, y: p.y, width: 0, height: 0 }); return; }
        e.currentTarget.setPointerCapture(e.pointerId);
        this.drag = { x0: p.x, y0: p.y, x: p.x, y: p.y };
      },
      stageMove: function (e) {
        if (this.pan) { this.panX = this.pan.px + (e.clientX - this.pan.x0); this.panY = this.pan.py + (e.clientY - this.pan.y0); return; }
        if (!this.drag) return;
        var p = this.toPct(e);
        this.drag.x = p.x; this.drag.y = p.y;
      },
      stageUp: function () {
        if (this.pan) { this.pan = null; return; }
        if (!this.drag) return;
        var b = this.rectOf(this.drag);
        this.drag = null;
        if (b.width >= this.data.min_size && b.height >= this.data.min_size) this.startDraft(Object.assign({ shape: this.tool }, b));
      },
      rectOf: function (d) {
        var r = function (v) { return Math.round(v * 100) / 100; };
        return { x: r(Math.min(d.x0, d.x)), y: r(Math.min(d.y0, d.y)), width: r(Math.abs(d.x - d.x0)), height: r(Math.abs(d.y - d.y0)) };
      },
      liveDraft: function () {
        if (this.drag && (this.tool === 'rect' || this.tool === 'ellipse')) return Object.assign({ shape: this.tool }, this.rectOf(this.drag));
        return this.editingId ? null : this.draft;
      },
      guessLocation: function (g) {
        var l = g.x, r = g.x + g.width;
        if (r <= 52) return '우폐';
        if (l >= 48) return '좌폐';
        return '양측';
      },
      startDraft: function (g) {
        var round = function (v) { return Math.round(v * 100) / 100; };
        g.x = round(g.x); g.y = round(g.y);
        if (this.cur.kind === 'volume') { g.plane = this.plane; g.slice = this.idx[this.plane]; } else { g.plane = null; g.slice = null; }
        this.draft = g; this.editingId = null; this.selectedId = null; this.formError = '';
        this.form = { finding_type: g.shape === 'text' ? '' : '침윤', location: this.guessLocation(g), severity: '중등도', text: '' };
        if (this.tab !== 0) this.setTab(0, TABS[0]);
      },
      editAnn: function (a) {
        this.draft = null; this.editingId = a.id; this.selectedId = a.id; this.formError = '';
        this.form = { finding_type: a.finding_type, location: a.location || '양측', severity: a.severity || '중등도', text: a.text };
      },
      clearEdit: function () { this.draft = null; this.editingId = null; this.formError = ''; },
      cancelForm: function () { this.clearEdit(); this.tool = 'select'; },
      formValid: function () {
        var g = this.formGeom;
        if (!g) return false;
        return g.shape === 'text' ? this.form.text.trim() !== '' : this.form.finding_type !== '';
      },

      /* ─── 서버 저장 (POST + CSRF) ─── */
      post: function (url, fields) {
        var body = new URLSearchParams();
        Object.keys(fields).forEach(function (k) { if (fields[k] !== null && fields[k] !== undefined) body.append(k, fields[k]); });
        return fetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrf(), 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json' },
                            body: body, credentials: 'same-origin' })
          .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, data: j }; }); });
      },
      saveForm: function () {
        if (!this.formValid() || this.busy) return;
        var self = this, f = this.form, payload = { finding_type: f.finding_type, location: f.finding_type ? f.location : '',
          severity: f.finding_type ? f.severity : '', text: f.text.trim() };
        var req;
        if (this.editingId) {
          req = this.post('/rap/annotations/' + this.editingId, payload);
        } else {
          var g = this.draft;
          req = this.post('/rap/annotations', Object.assign(payload, { study: this.cur.id, shape: g.shape, x: g.x, y: g.y,
            width: g.width, height: g.height, plane: g.plane || '', slice: g.plane ? g.slice : '' }));
        }
        this.busy = true;
        req.then(function (res) {
          self.busy = false;
          if (!res.ok) { self.formError = res.data.error || (self.en ? 'Could not save' : '저장하지 못했습니다'); return; }
          var a = res.data.annotation, i = self.annotations.findIndex(function (x) { return x.id === a.id; });
          if (i >= 0) self.annotations.splice(i, 1, a); else self.annotations.push(a);
          self.selectedId = a.id;
          self.clearEdit(); self.tool = 'select';
          window.showToast && window.showToast('success', self.en ? 'Expert finding saved' : '전문가 소견을 저장했습니다');
        }).catch(function () { self.busy = false; self.formError = self.en ? 'Network error' : '네트워크 오류'; });
      },
      removeAnn: function (a) {
        var self = this;
        this.post('/rap/annotations/' + a.id + '/delete', {}).then(function (res) {
          if (!res.ok) { window.showToast && window.showToast('error', res.data.error || 'Error'); return; }
          self.annotations = self.annotations.filter(function (x) { return x.id !== a.id; });
          if (self.selectedId === a.id) self.selectedId = null;
          if (self.editingId === a.id) self.editingId = null;
        });
      },
      canDeleteSelected: function () {
        var id = this.selectedId;
        var a = id && this.annotations.find(function (x) { return x.id === id; });
        return !!(a && a.can_edit);
      },
      removeSelected: function () {
        var id = this.selectedId, a = id && this.annotations.find(function (x) { return x.id === id; });
        if (a && a.can_edit) this.removeAnn(a);
      },
      onKey: function (e) {
        var el = e.target;
        if (el && el.closest && el.closest('input, textarea, select, [contenteditable="true"]')) return;
        if (e.key === 'Escape') { this.draft = null; this.editingId = null; this.tool = 'select'; }
        else if ((e.key === 'Delete' || e.key === 'Backspace') && this.selectedId) { e.preventDefault(); this.removeSelected(); }
      },

      /* ─── 타임시리즈 ─── */
      play: function () {
        var self = this;
        if (this.studies.length <= 1) return;
        this.playState = 'playing';
        clearInterval(this.playTimer);
        this.playTimer = setInterval(function () {
          if (self.tab !== 1) { self.pause(); return; }
          var next = (self.sel + 1) % self.studies.length;
          self.clearEdit(); self.selectedId = null; self.sel = next; self.resetVolume();
        }, 900);
      },
      pause: function () { this.playState = 'paused'; clearInterval(this.playTimer); this.playTimer = null; },
      stop: function () { this.playState = 'stopped'; clearInterval(this.playTimer); this.playTimer = null; this.selectStudy(0); },
      compareSummary: function () {
        var a = this.studies[this.cmpA], b = this.studies[this.cmpB];
        if (!a || !b) return '';
        var fa = a.findings.map(function (f) { return f.type_l; }), fb = b.findings.map(function (f) { return f.type_l; });
        var added = fb.filter(function (x) { return fa.indexOf(x) < 0; }), gone = fa.filter(function (x) { return fb.indexOf(x) < 0; });
        if (this.en) return 'Change A→B · new: ' + (added.join(', ') || 'none') + ' · resolved: ' + (gone.join(', ') || 'none');
        return '변화 A→B · 새 소견: ' + (added.join(', ') || '없음') + ' · 사라진 소견: ' + (gone.join(', ') || '없음');
      },
    };
  };
})();
