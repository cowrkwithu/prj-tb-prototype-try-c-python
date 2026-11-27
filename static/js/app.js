/* TBRS Platform 공통 스크립트 (빌드 없음) — 토스트, 도움말 툴팁 */
(function () {
  'use strict';

  /* ── 토스트 (원본 ToastProvider: 3초 후 자동 닫힘, 우하단) ── */
  function dismiss(el) {
    if (!el || el.classList.contains('toast-exit')) return;
    el.classList.add('toast-exit');
    setTimeout(function () { el.remove(); }, 200);
  }
  function arm(el) {
    if (el.dataset.armed) return;
    el.dataset.armed = '1';
    setTimeout(function () { dismiss(el); }, 3000);
  }
  function iconHtml(type) {
    var tpl = document.getElementById('toast-icons');
    if (!tpl) return '';
    var node = tpl.content.querySelector('[data-type="' + type + '"]');
    return node ? node.innerHTML : '';
  }
  window.showToast = function (type, message) {
    if (['success', 'error', 'info'].indexOf(type) < 0) type = 'info';
    var stack = document.getElementById('toast-stack');
    if (!stack) return;
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.setAttribute('data-toast', '');
    el.setAttribute('role', 'status');
    var msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = message;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('data-toast-close', '');
    close.setAttribute('aria-label', document.documentElement.lang === 'en' ? 'Dismiss notification' : '알림 닫기');
    close.innerHTML = iconHtml('close');
    el.innerHTML = iconHtml(type);
    el.appendChild(msg);
    el.appendChild(close);
    stack.appendChild(el);
    arm(el);
  };
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-toast-close]');
    if (btn) dismiss(btn.closest('[data-toast]'));
  });
  // 서버 응답 헤더 HX-Trigger: {"showToast": {"type": "success", "message": "…"}}
  document.addEventListener('showToast', function (e) {
    var d = e.detail || {};
    window.showToast(d.type, d.message || d.value || '');
  });

  /* ── 도움말 툴팁 (원본 HelpTip: 300ms 머무르면 표시, 화면 끝이면 반대로 뒤집음, 터치 제외) ── */
  var tip = null, timer = null, anchor = null;
  function ensureTip() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'helptip';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
    }
    return tip;
  }
  function place(el) {
    var t = ensureTip();
    var text = el.getAttribute('data-tip');
    if (!text) return;
    t.textContent = text;
    t.style.visibility = 'hidden';
    t.classList.add('show');
    var a = el.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
    var vw = window.innerWidth, vh = window.innerHeight, GAP = 8, M = 8, left, top;
    var clamp = function (v, lo, hi) { return Math.min(Math.max(v, lo), hi); };
    if (el.getAttribute('data-tip-side') === 'right') {
      left = (a.right + GAP + w > vw - M) ? a.left - GAP - w : a.right + GAP;
      top = clamp(a.top + a.height / 2 - h / 2, M, vh - h - M);
    } else {
      top = (a.bottom + GAP + h > vh - M) ? a.top - GAP - h : a.bottom + GAP;
      left = clamp(a.left + a.width / 2 - w / 2, M, vw - w - M);
    }
    t.style.left = left + 'px';
    t.style.top = top + 'px';
    t.style.visibility = 'visible';
  }
  function hide() {
    clearTimeout(timer);
    anchor = null;
    if (tip) tip.classList.remove('show');
  }
  document.addEventListener('pointerover', function (e) {
    if (e.pointerType !== 'mouse') return;
    var el = e.target.closest('[data-tip]');
    if (!el || el === anchor) return;
    hide();
    anchor = el;
    timer = setTimeout(function () { if (anchor === el) place(el); }, 300);
  });
  document.addEventListener('pointerout', function (e) {
    if (anchor && !anchor.contains(e.relatedTarget)) hide();
  });
  document.addEventListener('pointerdown', hide);
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip]');
    if (el && e.target.matches(':focus-visible')) { anchor = el; place(el); }
  });
  document.addEventListener('focusout', hide);
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);

  /* ── 카운트업 (원본 StatCard useCountUp: 1.2초 easeOutCubic, 화면에 30% 보이면 시작) ── */
  function countUp(el) {
    var target = parseInt(el.getAttribute('data-countup'), 10) || 0;
    var locale = document.documentElement.lang === 'en' ? 'en-US' : 'ko-KR';
    if (target <= 0) { el.textContent = (0).toLocaleString(locale); return; }
    var start = null, dur = 1200;
    function step(now) {
      if (start === null) start = now;
      var p = Math.min((now - start) / dur, 1);
      el.textContent = Math.floor((1 - Math.pow(1 - p, 3)) * target).toLocaleString(locale);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  function observe(selector, threshold, fn) {
    var els = document.querySelectorAll(selector);
    if (!('IntersectionObserver' in window)) { els.forEach(fn); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { io.unobserve(en.target); fn(en.target); } });
    }, { threshold: threshold });
    els.forEach(function (el) { io.observe(el); });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-toast]').forEach(arm);
    observe('[data-countup]', 0.3, countUp);
    // 원본 useFadeIn: 15% 보이면 서서히 나타남. JS 가 없으면 처음부터 보인다(.js-fade 가 붙을 때만 숨김).
    document.documentElement.classList.add('js-fade');
    observe('.fade-in', 0.15, function (el) { el.classList.add('is-visible'); });
  });
})();
