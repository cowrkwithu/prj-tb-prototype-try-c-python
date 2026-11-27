from django import forms

from .prediction import ALGO_IDS, EXTS, MAX_BYTES


class PredictionForm(forms.Form):
    """약제내성 예측 입력 — 대상자 선택 또는 변이 목록(파일·붙여넣기)."""

    source = forms.ChoiceField(choices=[("patient", "patient"), ("upload", "upload")])
    patient = forms.IntegerField(required=False, min_value=1, max_value=2_147_483_647)
    algorithms = forms.MultipleChoiceField(choices=[(a, a) for a in ALGO_IDS])
    assume_wgs = forms.BooleanField(required=False)
    text = forms.CharField(required=False, strip=False, max_length=MAX_BYTES)
    file = forms.FileField(required=False, max_length=255, allow_empty_file=True)

    def clean(self):
        data = super().clean()
        src = data.get("source")
        if src == "patient" and not data.get("patient"):
            self.add_error("patient", forms.ValidationError("대상자를 고르세요", code="required"))
        if src == "upload":
            f = data.get("file")
            if f is None and not (data.get("text") or "").strip():
                self.add_error("file", forms.ValidationError("변이 파일을 올리거나 내용을 붙여 넣으세요", code="required"))
            if f is not None:
                name = (f.name or "").lower()
                if not name.endswith(EXTS):
                    self.add_error("file", forms.ValidationError("지원하지 않는 형식입니다", code="ext"))
                elif f.size > MAX_BYTES:
                    self.add_error("file", forms.ValidationError("파일이 너무 큽니다", code="size"))
        return data
