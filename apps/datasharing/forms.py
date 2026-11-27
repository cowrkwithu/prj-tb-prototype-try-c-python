"""데이터 접근 신청(DAR)·API 키 발급 폼 — 원본 DarTab.validate / ApiTab 입력 규칙을 서버에서 그대로 검사한다.

오류 문구는 원본과 같은 한/영 문구(lang 인자로 고른다).
"""
from __future__ import annotations

import re

from django import forms

from . import catalog

# 원본 validateEmail /^[^\s@]+@[^\s@]+\.[^\s@]+$/ , validateOrcid /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/ (ASCII 숫자만)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ORCID_RE = re.compile(r"^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$")
PURPOSE_MIN = 30
PURPOSE_MAX = 5000


class _BilingualForm(forms.Form):
    def __init__(self, *args, lang: str = "ko", **kwargs):
        super().__init__(*args, **kwargs)
        self.lang = lang

    def tr(self, ko: str, en: str) -> str:
        return en if self.lang == "en" else ko

    def _required_text(self, name, ko, en, max_len):
        v = (self.cleaned_data.get(name) or "").strip()
        if not v:
            raise forms.ValidationError(self.tr(ko, en))
        if len(v) > max_len:
            raise forms.ValidationError(self.tr(f"최대 {max_len}자까지 입력할 수 있습니다", f"At most {max_len} characters"))
        return v

    def _email(self, name, max_len=254):
        v = (self.cleaned_data.get(name) or "").strip()
        if not v:
            raise forms.ValidationError(self.tr("이메일을 입력하세요", "Enter an email address"))
        if len(v) > max_len or not EMAIL_RE.match(v):
            raise forms.ValidationError(self.tr("올바른 이메일 형식이 아닙니다", "Invalid email format"))
        return v


class DarForm(_BilingualForm):
    # required=False + clean_* 에서 원본 문구로 검사(Django 기본 '필수 항목' 문구 대신)
    name = forms.CharField(required=False, strip=True)
    institution = forms.CharField(required=False, strip=True)
    email = forms.CharField(required=False, strip=True)
    orcid = forms.CharField(required=False, strip=True)
    approver_name = forms.CharField(required=False, strip=True)
    approver_title = forms.CharField(required=False, strip=True)
    approver_email = forms.CharField(required=False, strip=True)
    data_types = forms.MultipleChoiceField(required=False, choices=[(t, t) for t in catalog.DATA_TYPES])
    purpose = forms.CharField(required=False, strip=True)
    dua_agreed = forms.BooleanField(required=False)

    def clean_name(self):
        return self._required_text("name", "이름을 입력하세요", "Enter your name", 100)

    def clean_institution(self):
        return self._required_text("institution", "소속기관을 입력하세요", "Enter your institution", 200)

    def clean_email(self):
        return self._email("email")

    def clean_orcid(self):
        v = (self.cleaned_data.get("orcid") or "").strip()
        if v and not ORCID_RE.match(v):
            raise forms.ValidationError(self.tr("ORCID 형식: 0000-0000-0000-0000", "ORCID format: 0000-0000-0000-0000"))
        return v

    def clean_approver_name(self):
        return self._required_text("approver_name", "승인권자 이름을 입력하세요", "Enter the approver's name", 100)

    def clean_approver_title(self):
        return self._required_text("approver_title", "직책을 입력하세요", "Enter a title", 100)

    def clean_approver_email(self):
        return self._email("approver_email")

    def clean_data_types(self):
        v = self.cleaned_data.get("data_types") or []
        if not v:
            raise forms.ValidationError(self.tr("요청할 데이터 유형을 선택하세요", "Select the data types to request"))
        return [t for t in catalog.DATA_TYPES if t in v]

    def clean_purpose(self):
        v = (self.cleaned_data.get("purpose") or "").strip()
        if len(v) < PURPOSE_MIN:
            raise forms.ValidationError(self.tr("연구 목적을 30자 이상 작성하세요",
                                                "Describe the research purpose in at least 30 characters"))
        if len(v) > PURPOSE_MAX:
            raise forms.ValidationError(self.tr(f"최대 {PURPOSE_MAX}자까지 입력할 수 있습니다", f"At most {PURPOSE_MAX} characters"))
        return v

    def clean_dua_agreed(self):
        if not self.cleaned_data.get("dua_agreed"):
            raise forms.ValidationError(self.tr("DUA 동의가 필요합니다", "You must agree to the DUA"))
        return True

    def error_map(self) -> dict:
        return {k: v[0] for k, v in self.errors.items()}


class ApiKeyForm(_BilingualForm):
    """원본 ApiTab '이메일 *'·'소속기관 *'."""

    email = forms.CharField(required=False, strip=True)
    institution = forms.CharField(required=False, strip=True)

    def clean_email(self):
        return self._email("email")

    def clean_institution(self):
        return self._required_text("institution", "소속기관을 입력하세요", "Enter your institution", 200)
