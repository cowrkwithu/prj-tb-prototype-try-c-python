/* 표준 데이터사전 — AI 도우미 결과를 메인 목록 검색에 적용, 대화창 자동 스크롤 */
(function () {
  'use strict';
  window.dictPage = function () {
    return {
      applySearch: function (q) {
        // 원본: 도우미 검색 시 타입 필터를 비우고 같은 검색어를 메인 목록에 적용
        document.querySelectorAll('.dict-state[name=type]').forEach(function (el) { el.remove(); });
        var input = document.getElementById('dict-q');
        if (!input) return;
        input.value = q;
        htmx.trigger(document.getElementById('dict-form'), 'submit');
      },
    };
  };
  // HX-Trigger: {"dictApplySearch": {"q": ...}} → Alpine 이 듣는 window 이벤트로 전달
  document.body.addEventListener('dictApplySearch', function (e) {
    window.dispatchEvent(new CustomEvent('dict-apply-search', { detail: e.detail }));
  });
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (e.detail.target && e.detail.target.id === 'ai-messages') setBusy(true);
  });
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (e.detail.target && e.detail.target.id === 'ai-messages') {
      setBusy(false);
      e.detail.target.scrollTo({ top: e.detail.target.scrollHeight, behavior: 'smooth' });
    }
  });
  // 서버 오류 응답·네트워크 오류(sendError) 모두 '입력 중' 상태를 풀어 준다
  document.body.addEventListener('htmx:responseError', function () { setBusy(false); });
  document.body.addEventListener('htmx:sendError', function () { setBusy(false); });
  function setBusy(v) {
    var root = document.querySelector('.ai-assist');
    if (root && root._x_dataStack) root._x_dataStack[0].busy = v;
  }
})();
