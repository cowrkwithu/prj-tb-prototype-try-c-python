/* 데이터 정의 화면 스크립트 (빌드 없음, Alpine.js 컴포넌트)
 * - schemaBuilder: 원본 SchemaBuilder 의 리듀서(ADD/REMOVE/UPDATE/REORDER) — 드래그는 HTML5 네이티브 DnD
 *   (원본 dnd-kit 대체, 별도 라이브러리 불필요) + 위/아래 버튼 + 키보드(Alt+↑/↓)
 * - collectForm: 미리보기 폼 입력값을 JSON 으로 (원본 PreviewModal 실시간 JSON)
 * - dfForm: 동적 폼 입력 진행률 (원본 DynamicForm progress bar)
 * 저장·검증은 모두 서버가 한다. 여기서 만든 JSON 은 서버가 다시 검사한다.
 */
(function () {
  'use strict';

  function readJson(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent); } catch (e) { return fallback; }
  }
  function uid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '').slice(0, 12);
    return Math.random().toString(16).slice(2, 14);
  }
  var isEn = function () { return document.documentElement.lang === 'en'; };

  window.schemaBuilder = function () {
    var state = readJson('builder-state', { meta: {}, fields: [] });
    var fields = (state.fields || []).map(function (f) {
      var c = f.constraints || {};
      if (Array.isArray(c.allowedFileTypes)) c.allowedFileTypes = c.allowedFileTypes.join(', ');
      f.constraints = c;
      f._origName = f.fieldName;
      if (!f.fieldId) f.fieldId = uid();
      return f;
    });
    return {
      meta: state.meta || {},
      fields: fields,
      types: readJson('builder-types', {}),
      withData: readJson('builder-with-data', []),
      codes: readJson('builder-codes', []),
      activeKey: null,
      leftTab: 'types',
      metaOpen: true,
      collapsed: {},
      canvasOver: false,
      overKey: null,
      pendingType: null,
      previewOpen: false,
      showJson: true,
      previewJson: '{}',
      dirty: false,
      init: function () {
        // ?field=<필드명> 이면 그 필드를 골라 둔다(링크 공유·검토용)
        var want = new URLSearchParams(location.search).get('field');
        var hit = want && this.fields.filter(function (f) { return f.fieldName === want; })[0];
        if (hit) this.activeKey = hit.fieldId;
      },

      touch: function (modified) {
        this.dirty = true;
        var a = this.active();
        if (modified && a && a.sourceRef) a.sourceRef.isModified = true;
      },
      typeColor: function (t) { return (this.types[t] && this.types[t].color) || '#94A3B8'; },
      label: function (f) { return (isEn() && f.displayNameEn) ? f.displayNameEn : (f.displayName || f.fieldName); },
      active: function () {
        var k = this.activeKey;
        for (var i = 0; i < this.fields.length; i++) if (this.fields[i].fieldId === k) return this.fields[i];
        return null;
      },
      indexOf: function (key) {
        for (var i = 0; i < this.fields.length; i++) if (this.fields[i].fieldId === key) return i;
        return -1;
      },
      groupLabel: function (g) {
        var defs = this.meta.fieldGroups || [];
        for (var i = 0; i < defs.length; i++) {
          if (defs[i].groupName === g) return (isEn() && defs[i].displayNameEn) || defs[i].displayName || g;
        }
        return g;
      },
      rows: function () {
        var out = [], last, self = this;
        this.fields.forEach(function (f, i) {
          var g = f.group || '';
          if (g && g !== last) out.push({ kind: 'group', group: g, label: self.groupLabel(g), key: 'g:' + g + ':' + i });
          last = g;
          out.push({ kind: 'field', f: f, idx: i, key: 'f:' + f.fieldId });
        });
        return out;
      },
      toggleGroup: function (g) { this.collapsed[g] = !this.collapsed[g]; },
      insert: function (field, index) {
        var at = (index == null || index > this.fields.length) ? this.fields.length : index;
        this.fields.splice(at, 0, field);
        this.activeKey = field.fieldId;
        this.dirty = true;
      },
      addType: function (type, index) {
        var t = this.types[type] || { label: type };
        var count = this.fields.filter(function (f) { return f.dataType === type; }).length + 1;
        this.insert({
          fieldId: uid(), fieldName: type.toLowerCase() + '_field_' + count,
          displayName: isEn() ? (t.label + ' Field ' + count) : (t.label + ' 필드 ' + count),
          displayNameEn: '', description: '', dataType: type, constraints: {}, isRequired: false, isUnique: false, group: ''
        }, index);
      },
      addDict: function (e, index) {
        var c = JSON.parse(JSON.stringify(e.constraints || {}));
        this.insert({
          fieldId: uid(), fieldName: e.fieldName, displayName: e.displayName, displayNameEn: e.displayNameEn || '',
          description: e.description || '', descriptionEn: e.descriptionEn || '', dataType: e.dataType, constraints: c,
          isRequired: false, isUnique: false, group: '',
          sourceRef: { dictionaryId: e.dictionaryId, fieldId: e.dictionaryId, fieldName: e.fieldName, isModified: false }
        }, index);
        if (window.showToast) window.showToast('success', isEn() ? ('Added ' + (e.displayNameEn || e.displayName)) : ("'" + e.displayName + "' 필드를 추가했습니다"));
      },
      remove: function (key) {
        var i = this.indexOf(key);
        if (i < 0) return;
        this.fields.splice(i, 1);
        if (this.activeKey === key) this.activeKey = null;
        this.dirty = true;
      },
      duplicate: function (key) {
        var i = this.indexOf(key);
        if (i < 0) return;
        var copy = JSON.parse(JSON.stringify(this.fields[i]));
        copy.fieldId = uid();
        copy.fieldName = copy.fieldName + '_copy';
        delete copy._origName;
        this.insert(copy, i + 1);
      },
      move: function (key, dir) {
        var i = this.indexOf(key), j = i + dir;
        if (i < 0 || j < 0 || j >= this.fields.length) return;
        var f = this.fields.splice(i, 1)[0];
        this.fields.splice(j, 0, f);
        this.dirty = true;
        var self = this;
        this.$nextTick(function () {
          var el = self.$root.querySelector('[data-testid="field-' + f.fieldName + '"]');
          if (el) el.focus();
        });
      },
      select: function (key) { this.activeKey = key; },
      changeType: function (ev) {
        var a = this.active(), t = ev.target.value;
        if (!a || t === a.dataType) return;
        var hasCons = Object.keys(a.constraints || {}).some(function (k) { var v = a.constraints[k]; return v !== '' && v != null && !(Array.isArray(v) && !v.length); });
        if (hasCons) { this.pendingType = t; ev.target.value = a.dataType; } else { this.applyType(t); }
      },
      applyType: function (t) {
        var a = this.active();
        if (!a) return;
        a.dataType = t;
        a.constraints = {};
        this.touch(true);
      },
      dragStart: function (ev, data) {
        ev.dataTransfer.effectAllowed = 'copyMove';
        ev.dataTransfer.setData('text/plain', JSON.stringify(data));
      },
      dragDict: function (ev) {
        var raw = ev.currentTarget.getAttribute('data-entry');
        ev.dataTransfer.effectAllowed = 'copy';
        ev.dataTransfer.setData('text/plain', JSON.stringify({ origin: 'dictionary', entry: JSON.parse(raw) }));
      },
      drop: function (ev, index) {
        this.canvasOver = false;
        this.overKey = null;
        var data;
        try { data = JSON.parse(ev.dataTransfer.getData('text/plain')); } catch (e) { return; }
        if (!data) return;
        if (data.origin === 'palette') this.addType(data.type, index);
        else if (data.origin === 'dictionary') this.addDict(data.entry, index);
        else if (data.origin === 'canvas') {
          var from = this.indexOf(data.key);
          if (from < 0) return;
          var f = this.fields.splice(from, 1)[0];
          var to = from < index ? index - 1 : index;
          this.fields.splice(Math.min(to, this.fields.length), 0, f);
          this.dirty = true;
        }
      },
      payload: function () {
        var meta = JSON.parse(JSON.stringify(this.meta));
        var fields = this.fields.map(function (f, i) {
          var c = JSON.parse(JSON.stringify(f));
          delete c._origName;
          c.order = i;
          Object.keys(c.constraints || {}).forEach(function (k) {
            if (c.constraints[k] === '' || c.constraints[k] == null) delete c.constraints[k];
          });
          return c;
        });
        return JSON.stringify({ meta: meta, fields: fields });
      },
      save: function (action) {
        this.$refs.payload.value = this.payload();
        this.$refs.action.value = action;
        this.dirty = false;
        this.$refs.saveForm.submit();
      },
      openPreview: function () { this.previewOpen = true; this.loadPreview(false); },
      loadPreview: function (fill) {
        var token = document.querySelector('meta[name="csrf-token"]');
        htmx.ajax('POST', '/schemas/builder/preview', {
          target: '#preview-body', swap: 'innerHTML', source: '#preview-body',
          values: { payload: this.payload(), fill: fill ? '1' : '', csrfmiddlewaretoken: token ? token.content : '' }
        });
      },
      collectForm: window.collectForm
    };
  };

  /* 폼 입력 → {fieldName: 값} (미리보기 JSON) */
  window.collectForm = function (root) {
    var out = {};
    if (!root) return '{}';
    root.querySelectorAll('[data-field]').forEach(function (wrap) {
      var name = wrap.getAttribute('data-field'), type = wrap.getAttribute('data-type');
      var inputs = wrap.querySelectorAll('input, select, textarea');
      if (type === 'MULTISELECT') {
        var vals = [];
        inputs.forEach(function (i) { if (i.checked) vals.push(i.value); });
        if (vals.length) out[name] = vals;
      } else if (type === 'NUMERIC_RANGE') {
        var r = {};
        inputs.forEach(function (i) { if (i.value !== '') r[i.name.slice(-3)] = parseFloat(i.value); });
        if (Object.keys(r).length) out[name] = r;
      } else if (type === 'BOOLEAN') {
        inputs.forEach(function (i) { if (i.checked && i.value) out[name] = i.value === 'true'; });
      } else {
        inputs.forEach(function (i) {
          if (i.type === 'file' || i.type === 'hidden' || i.value === '') return;
          out[name] = (type === 'INTEGER') ? parseInt(i.value, 10) : (type === 'DECIMAL' ? parseFloat(i.value) : i.value);
        });
      }
    });
    return JSON.stringify(out, null, 2);
  };

  /* 동적 폼 진행률 */
  window.dfForm = function () {
    return {
      total: 0, filled: 0, req: 0, reqFilled: 0, groups: {},
      init: function () { var self = this; this.$nextTick(function () { self.recount(); }); },
      isFilled: function (wrap) {
        var ok = false;
        wrap.querySelectorAll('input, select, textarea').forEach(function (i) {
          if (i.type === 'checkbox' || i.type === 'radio') { if (i.checked && i.value) ok = true; }
          else if (i.type === 'file') { if (i.files && i.files.length) ok = true; }
          else if (i.type === 'hidden') { if (i.value === '1' && i.name.slice(-6) === '__keep') ok = true; }
          else if (i.value !== '' && !i.readOnly) ok = true;
        });
        return ok;
      },
      recount: function () {
        var self = this, t = 0, f = 0, r = 0, rf = 0, groups = {};
        this.$root.querySelectorAll('[data-field]').forEach(function (w) {
          if (w.getAttribute('data-type') === 'CALCULATED') return;
          var g = w.getAttribute('data-group') || '_default';
          groups[g] = groups[g] || { total: 0, filled: 0 };
          var ok = self.isFilled(w);
          t++; groups[g].total++;
          if (ok) { f++; groups[g].filled++; }
          if (w.getAttribute('data-required') === '1') { r++; if (ok) rf++; }
        });
        this.total = t; this.filled = f; this.req = r; this.reqFilled = rf; this.groups = groups;
      },
      g: function (k) { return this.groups[k] || { total: 0, filled: 0 }; }
    };
  };
})();
