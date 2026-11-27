from django import forms

from .models import User


class LoginForm(forms.Form):
    username = forms.CharField(max_length=150, required=False)
    password = forms.CharField(max_length=128, required=False, strip=False)


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        # 소속 기관은 '기관 공유' 데이터 구조의 열람 범위를 정하므로 본인이 바꿀 수 없다(관리자만 /django-admin 에서 변경)
        fields = ("display_name", "email")

    def clean_display_name(self):
        v = (self.cleaned_data.get("display_name") or "").strip()
        if not v:
            raise forms.ValidationError("이름을 입력하세요 · Enter your name")
        return v
