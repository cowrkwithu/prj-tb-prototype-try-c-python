"""쓰기 요청 한 건 = DB 트랜잭션 한 번 (5단계 리뷰 — 503 안내 '저장되지 않았습니다' 를 참으로).

`atomic_write` 로 감싼 뷰는 전체가 transaction.atomic 한 블록이다. 중간에 잠금 오류(503) 등 예외가 나면
DB 쓰기는 모두 롤백되고, 이 요청이 새로 저장한 파일(register_new_file 로 알린 것)도 지운다 — DB 가 참조하지 않는
파일이 남지 않게. 지우지 못한 파일은 cleanup_media(참조 없는 파일 정리)가 나중에 지운다.
기존 파일의 삭제는 반대로 커밋 뒤에만 한다(delete_after_commit) — 롤백되면 행과 파일이 함께 남는다.

전역 ATOMIC_REQUESTS 는 쓰지 않는다: 업로드 승인·삭제 승인은 자체 잠금 순서·재시도(savepoint) 구조를 가진다.
"""
from __future__ import annotations

import contextvars
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.db import transaction

_NEW_FILES: contextvars.ContextVar[list | None] = contextvars.ContextVar("tb_new_files", default=None)


def register_new_file(rel: str) -> None:
    """MEDIA_ROOT 기준 상대 경로 — atomic_write 블록이 실패하면 지울 새 파일."""
    files = _NEW_FILES.get()
    if files is not None:
        files.append(rel)


def _remove_quietly(rel: str) -> None:
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:  # Windows 파일 잠금 등 — cleanup_media 가 참조 없는 파일로 지운다
        pass


def delete_after_commit(paths, deleter) -> None:
    """기존 파일 삭제는 DB 삭제가 커밋된 뒤에(트랜잭션 밖이면 바로).

    robust=True: 커밋 뒤 정리가 실패해도(파일 잠금·OrphanFile 기록 실패 등) 예외를 올리지 않고 로그만 남긴다 —
    이미 커밋된 요청의 응답(성공)을 바꾸지 않는다. 남은 파일은 DB 가 참조하지 않으므로 cleanup_media 가 회수한다."""
    paths = [p for p in paths if p]
    if paths:
        transaction.on_commit(lambda: [deleter(p) for p in paths], robust=True)


def atomic_write(view):
    """뷰 전체를 한 트랜잭션으로. 예외(잠금 503 포함)로 롤백되면 이 요청이 저장한 새 파일을 지운다."""

    @wraps(view)
    def inner(request, *args, **kwargs):
        if request.method == "POST":
            # 본문(대용량 업로드 포함) 수신·파싱은 트랜잭션 밖에서 — SQLite 는 트랜잭션 시작(BEGIN IMMEDIATE)부터
            # 쓰기 잠금을 잡으므로 수신 시간 동안 다른 쓰기를 막지 않게 한다. 파싱 오류는 뷰가 평소처럼 처리한다.
            try:
                request.POST  # noqa: B018
                request.FILES  # noqa: B018
            except Exception:  # noqa: BLE001
                pass
        token = _NEW_FILES.set([])
        state = {"committed": False}
        try:
            with transaction.atomic():
                # 커밋 표시 — on_commit 콜백은 등록 순서대로 커밋 직후에 실행되므로 가장 먼저 등록한다.
                # 커밋된 뒤에 난 예외(다른 커밋 후 콜백 등)에서는 새 파일을 지우지 않는다(DB 행이 참조한다).
                transaction.on_commit(lambda: state.update(committed=True))
                return view(request, *args, **kwargs)
        except BaseException:
            if not state["committed"]:  # 롤백된 경우에만 — 이 요청이 저장한 새 파일 정리
                for rel in _NEW_FILES.get() or []:
                    _remove_quietly(rel)
            raise
        finally:
            _NEW_FILES.reset(token)

    return inner
