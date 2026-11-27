"""DICOM 헤더 최소 판독기 — 비식별화 검증용 (외부 의존성 없음).

원본 ImagingUpload 는 고정된 표(모든 태그 '안전')를 보여 주는 목업이었고, deidentify.checkDicomHeaders 는
'파서 도입 전까지 빈 결과'였다. 여기서는 같은 6개 태그(Patient Name/ID/Birth Date/Other IDs, Institution Name,
Referring Physician)를 실제 파일에서 읽는다. 픽셀 데이터나 그 밖의 태그는 해석하지 않는다.

지원: 128바이트 프리앰블 + 'DICM' 이 있는 Part 10 파일, Explicit/Implicit VR Little Endian,
Explicit VR Big Endian. 정의되지 않은 길이(SQ)는 항목 구분자까지 건너뛴다. 압축(Deflate) 데이터셋은 판독 불가로 표시한다.
"""
from __future__ import annotations

import struct
import time

MAX_VALUE_READ = 64 * 1024     # 대상 태그 값은 최대 64KB 만 읽는다
MAX_ELEMENTS = 200_000         # 비정상 파일로 인한 과도한 순회 방지(모든 순회 경로에서 센다)
MAX_DEPTH = 32                 # 정의되지 않은 길이 SQ/항목 중첩 깊이 상한(재귀 폭주 방지)
_LONG_VRS = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV", b"UC", b"UR", b"UT", b"UN", b"UV"}
EXPLICIT_LE = "1.2.840.10008.1.2.1"
IMPLICIT_LE = "1.2.840.10008.1.2"
EXPLICIT_BE = "1.2.840.10008.1.2.2"
DEFLATED = "1.2.840.10008.1.2.1.99"


class DicomError(ValueError):
    def __init__(self, ko: str, en: str):
        super().__init__(ko)
        self.ko, self.en = ko, en


def has_dicm_magic(head: bytes) -> bool:
    return len(head) >= 132 and head[128:132] == b"DICM"


class _Stream:
    """파일을 seek 로 순회한다 — 큰 요소 값(픽셀·사설 OB 등)은 읽지 않고 건너뛴다(메모리에 올리지 않음)."""

    def __init__(self, f):
        self.f = f
        f.seek(0, 2)
        self.size = f.tell()
        f.seek(0)
        self.count = 0
        from django.conf import settings

        self.deadline = time.monotonic() + float(getattr(settings, "DICOM_PARSE_TIMEOUT_SECONDS", 2.0))

    def tick(self):
        """요소 1개를 셀 때마다: 요소 수 상한·판독 시간 상한 검사."""
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise DicomError("DICOM 요소가 너무 많습니다", "Too many DICOM elements")
        if self.count % 1024 == 0 and time.monotonic() > self.deadline:
            raise DicomError("DICOM 헤더 판독 시간이 상한을 넘었습니다", "DICOM header parsing exceeded the time limit")

    def tell(self):
        return self.f.tell()

    def read(self, n):
        data = self.f.read(n)
        if len(data) < n:
            raise DicomError("DICOM 헤더가 중간에 잘렸습니다", "The DICOM header is truncated")
        return data

    def skip(self, n):
        if self.tell() + n > self.size:
            raise DicomError("DICOM 요소 길이가 파일 크기를 넘습니다(잘린 파일)", "A DICOM element runs past the end of file (truncated)")
        self.f.seek(n, 1)

    def eof(self):
        return self.tell() >= self.size


def read_tags(fileobj, wanted: set[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """wanted 태그의 문자열 값을 읽는다. 대상 그룹을 지날 때까지 실제로 판독하지 못하면 DicomError."""
    st = _Stream(fileobj)
    if not has_dicm_magic(st.read(132)):
        raise DicomError("DICM 매직바이트 없음", "No DICM magic bytes")
    found: dict[tuple[int, int], str] = {}
    transfer = EXPLICIT_LE  # 파일 메타 정보(그룹 0002)는 항상 Explicit VR Little Endian
    while not st.eof():
        pos = st.tell()
        group = struct.unpack("<H", st.read(2))[0]
        st.f.seek(pos)
        if group != 0x0002:
            break
        tag, vr, length = _header(st, explicit=True, little=True)
        if length == 0xFFFFFFFF:
            raise DicomError("메타 정보 길이 오류", "Invalid meta information length")
        if tag == (0x0002, 0x0010):
            transfer = st.read(length).decode("ascii", "ignore").strip("\x00 ")
        else:
            st.skip(length)
    if transfer == DEFLATED:
        raise DicomError("Deflate 압축 데이터셋은 판독하지 않습니다", "Deflated datasets are not supported")
    explicit = transfer != IMPLICIT_LE
    little = transfer != EXPLICIT_BE
    max_group = max(g for g, _ in wanted) if wanted else 0
    _dataset(st, explicit, little, wanted, found, max_group, top=True)
    return found


def _header(st, explicit, little):
    endian = "<" if little else ">"
    group, elem = struct.unpack(endian + "HH", st.read(4))
    st.tick()
    if group == 0xFFFE:  # 항목·구분자는 VR 없이 4바이트 길이
        return (group, elem), None, struct.unpack(endian + "I", st.read(4))[0]
    if explicit:
        vr = st.read(2)
        if vr in _LONG_VRS:
            st.read(2)
            length = struct.unpack(endian + "I", st.read(4))[0]
        else:
            length = struct.unpack(endian + "H", st.read(2))[0]
        return (group, elem), vr, length
    return (group, elem), None, struct.unpack(endian + "I", st.read(4))[0]


def _dataset(st, explicit, little, wanted, found, max_group, top=False, depth=0):
    """top 수준: 대상 그룹을 지나면(태그는 오름차순) 멈춘다. 하위(항목) 수준: 항목 구분자까지."""
    while True:
        if st.eof():
            if top:
                return  # 요소 경계에서 끝남 — 대상 태그가 없는 것이다
            raise DicomError("시퀀스 항목이 잘렸습니다", "A sequence item is truncated")
        tag, vr, length = _header(st, explicit, little)
        if tag in ((0xFFFE, 0xE00D), (0xFFFE, 0xE0DD)):
            return
        if top and tag[0] > max_group:
            return
        if length == 0xFFFFFFFF:
            _skip_sequence(st, explicit, little, depth + 1)
            continue
        if top and tag in wanted:
            raw = st.read(min(length, MAX_VALUE_READ))
            if length > MAX_VALUE_READ:
                st.skip(length - MAX_VALUE_READ)
            found[tag] = _decode(raw)
        else:
            st.skip(length)


def _skip_sequence(st, explicit, little, depth):
    """정의되지 않은 길이 SQ(또는 캡슐화 데이터): 항목을 차례로 건너뛰고 시퀀스 구분자 다음에서 멈춘다."""
    if depth > MAX_DEPTH:
        raise DicomError("DICOM 시퀀스 중첩이 너무 깊습니다", "DICOM sequences are nested too deeply")
    endian = "<" if little else ">"
    while True:
        group, elem = struct.unpack(endian + "HH", st.read(4))
        length = struct.unpack(endian + "I", st.read(4))[0]
        st.tick()
        if (group, elem) == (0xFFFE, 0xE0DD):
            return
        if (group, elem) != (0xFFFE, 0xE000):
            raise DicomError("시퀀스 구조 오류", "Invalid sequence structure")
        if length == 0xFFFFFFFF:
            _dataset(st, explicit, little, set(), {}, 0, depth=depth)
        else:
            st.skip(length)


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.replace("^", " ").strip("\x00 ").strip()


def inspect(fileobj) -> dict:
    """비식별화 검증 결과: {"parsed": bool, "error": str|None, "tags": [{tag, name, value, risky}]}"""
    from .schemas import DICOM_TAGS

    wanted = {(g, e) for g, e, _, _ in DICOM_TAGS}
    try:
        found = read_tags(fileobj, wanted)
    except DicomError as exc:
        return {"parsed": False, "error": exc.ko, "error_en": exc.en, "tags": []}
    except (struct.error, OSError, RecursionError, MemoryError):
        return {"parsed": False, "error": "DICOM 헤더 구조를 해석할 수 없습니다", "error_en": "Cannot parse the DICOM header", "tags": []}
    tags = []
    for g, e, name, identifying in DICOM_TAGS:
        value = found.get((g, e), "")
        tags.append({"tag": f"({g:04X},{e:04X})", "name": name, "value": value,
                     "risky": bool(identifying and value)})
    return {"parsed": True, "error": None, "tags": tags}


# ─── 테스트·데모용 최소 DICOM 생성기 ───
def build_minimal_dicom(tags: dict[tuple[int, int], str], explicit: bool = True, private_blob: int = 0) -> bytes:
    """주어진 문자열 태그로 Part 10 파일을 만든다(픽셀 없음). 테스트에서 실제 판독 경로를 검증하는 데 쓴다."""
    transfer = (EXPLICIT_LE if explicit else IMPLICIT_LE).encode("ascii")
    if len(transfer) % 2:
        transfer += b"\x00"
    meta_el = struct.pack("<HH", 0x0002, 0x0010) + b"UI" + struct.pack("<H", len(transfer)) + transfer
    meta_len = struct.pack("<HH", 0x0002, 0x0000) + b"UL" + struct.pack("<H", 4) + struct.pack("<I", len(meta_el))
    body = b""
    # 기관 정보 앞에 정의되지 않은 길이의 SQ 하나를 넣어 건너뛰기 경로도 거친다
    seq_item = struct.pack("<HH", 0x0008, 0x0100) + (b"SH" + struct.pack("<H", 4) if explicit else struct.pack("<I", 4)) + b"T123"
    sq = struct.pack("<HH", 0x0008, 0x1032) + ((b"SQ\x00\x00" + struct.pack("<I", 0xFFFFFFFF)) if explicit
                                                else struct.pack("<I", 0xFFFFFFFF))
    sq += struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + seq_item + struct.pack("<HHI", 0xFFFE, 0xE00D, 0)
    sq += struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
    elements = sorted(tags.items())
    # private_blob: 대상 태그 앞(그룹 0009)에 큰 사설 OB 값 — 판독 한도 우회 검증용
    blob = (struct.pack("<HH", 0x0009, 0x1010) + ((b"OB\x00\x00" + struct.pack("<I", private_blob)) if explicit
                                                 else struct.pack("<I", private_blob)) + b"\x00" * private_blob) if private_blob else b""
    inserted = blob_done = False
    for (g, e), value in elements:
        if not inserted and (g, e) > (0x0008, 0x1032):
            body += sq
            inserted = True
        if not blob_done and g >= 0x0010:
            body += blob
            blob_done = True
        raw = value.encode("utf-8")
        if len(raw) % 2:
            raw += b" "
        vr = b"PN" if (g, e) in ((0x0010, 0x0010), (0x0008, 0x0090)) else b"LO"
        if explicit:
            body += struct.pack("<HH", g, e) + vr + struct.pack("<H", len(raw)) + raw
        else:
            body += struct.pack("<HHI", g, e, len(raw)) + raw
    if not inserted:
        body += sq
    if not blob_done:
        body += blob
    # 픽셀 데이터 자리(그룹 7FE0) — 판독기는 여기까지 오기 전에 멈춘다
    body += struct.pack("<HH", 0x7FE0, 0x0010) + (b"OW\x00\x00" + struct.pack("<I", 4) if explicit else struct.pack("<I", 4)) + b"\x00\x00\x00\x00"
    return b"\x00" * 128 + b"DICM" + meta_len + meta_el + body
