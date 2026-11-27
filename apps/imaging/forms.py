"""전문가 소견 입력 검증 (원본 ExpertForm·ExpertOverlay 규칙을 서버에서 강제)."""
from django import forms

from .models import ExpertAnnotation
from .services import MIN_SIZE, NOTE_MAX, TEXT_MAX

EPS = 1e-6


class _Coord(forms.FloatField):
    def __init__(self, **kw):
        super().__init__(min_value=0, max_value=100, **kw)


class AnnotationInputForm(forms.Form):
    """수정 시에도 쓰는 내용 필드(소견 유형·위치·중증도·텍스트)."""

    finding_type = forms.CharField(required=False, max_length=20)
    location = forms.CharField(required=False, max_length=10)
    severity = forms.CharField(required=False, max_length=10)
    text = forms.CharField(required=False, max_length=NOTE_MAX)

    shape_value = None  # 하위 클래스/호출자가 정한다

    def clean_finding_type(self):
        v = self.cleaned_data.get("finding_type", "")
        if v and v not in ExpertAnnotation.FINDING_OPTIONS:
            raise forms.ValidationError("알 수 없는 소견 유형", code="finding")
        return v

    def clean_location(self):
        v = self.cleaned_data.get("location", "")
        if v and v not in ExpertAnnotation.LOCATIONS:
            raise forms.ValidationError("알 수 없는 위치", code="location")
        return v

    def clean_severity(self):
        v = self.cleaned_data.get("severity", "")
        if v and v not in ExpertAnnotation.SEVERITIES:
            raise forms.ValidationError("알 수 없는 중증도", code="severity")
        return v

    def clean_text(self):
        v = (self.cleaned_data.get("text") or "").strip()
        if any(ord(c) < 32 and c not in "\n\t" for c in v):
            raise forms.ValidationError("허용되지 않는 문자", code="chars")
        return v

    def check_content(self, shape: str):
        d = self.cleaned_data
        if shape == ExpertAnnotation.Shape.TEXT:
            if not d.get("text"):
                self.add_error("text", forms.ValidationError("텍스트 필요", code="text_required"))
            elif len(d["text"]) > TEXT_MAX:
                self.add_error("text", forms.ValidationError("텍스트 길이", code="text_long"))
        elif not d.get("finding_type"):
            self.add_error("finding_type", forms.ValidationError("소견 유형 필요", code="finding_required"))
        if d.get("finding_type"):
            d["location"] = d.get("location") or "양측"
            d["severity"] = d.get("severity") or "중등도"
        else:
            d["location"] = d["severity"] = ""


class AnnotationCreateForm(AnnotationInputForm):
    study = forms.IntegerField(min_value=1, max_value=2_147_483_647)
    shape = forms.ChoiceField(choices=ExpertAnnotation.Shape.choices)
    x = _Coord()
    y = _Coord()
    width = _Coord(required=False)
    height = _Coord(required=False)
    plane = forms.ChoiceField(required=False, choices=ExpertAnnotation.Plane.choices)
    slice = forms.IntegerField(required=False, min_value=0, max_value=10_000)

    def clean(self):
        d = super().clean()
        shape = d.get("shape")
        if shape is None or any(k not in d for k in ("x", "y")):
            return d
        w, h = d.get("width") or 0.0, d.get("height") or 0.0
        d["width"], d["height"] = w, h
        if shape == ExpertAnnotation.Shape.TEXT:
            if w or h:
                self.add_error("width", forms.ValidationError("텍스트는 점", code="text_size"))
        else:
            if w < MIN_SIZE or h < MIN_SIZE:
                self.add_error("width", forms.ValidationError("너무 작음", code="too_small"))
            if d["x"] + w > 100 + EPS or d["y"] + h > 100 + EPS:
                self.add_error("width", forms.ValidationError("영상 밖", code="out_of_bounds"))
        self.check_content(shape)
        return d
