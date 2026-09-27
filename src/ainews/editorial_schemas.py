"""Structured model outputs. Everything the model returns is validated here before use."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from rapidfuzz import fuzz

from .textutil import has_cyrillic

_URL = re.compile(r"https?://|www\.|t\.me/", re.I)
_TAG = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_]{2,30}$")

Category = Literal[
    "model_release", "product", "research", "business", "policy", "safety", "open_source", "hardware", "tools",
    "other",
]


def _ru(value: str, field: str) -> str:
    value = (value or "").strip()
    if value and not has_cyrillic(value):
        raise ValueError(f"{field}: text must be in Russian")
    if _URL.search(value):
        raise ValueError(f"{field}: links are not allowed in generated text")
    return value


class TriageItem(BaseModel):
    id: int
    is_ai_news: bool = Field(description="Событие действительно об ИИ/ML (не просто упоминание слова AI)")
    newsworthy: bool = Field(description="Новость, а не реклама, туториал, вакансия, мнение без события")
    category: Category
    rumor: bool = Field(description="Слух, утечка, неподтверждённые данные, 'по данным источников'")
    scale: int = Field(ge=0, le=10, description="Масштаб события")
    industry_impact: int = Field(ge=0, le=10, description="Влияние на индустрию ИИ")
    practical_value: int = Field(ge=0, le=10, description="Практическая польза для пользователей/разработчиков")
    broad_interest: int = Field(ge=0, le=10, description="Интерес для широкой аудитории")
    reason: str = Field(max_length=240, description="Краткое обоснование на русском")


class TriageResponse(BaseModel):
    items: list[TriageItem]


class DedupVerdict(BaseModel):
    pair: int
    same_event: bool
    reason: str = Field(max_length=200)


class DedupResponse(BaseModel):
    verdicts: list[DedupVerdict]


class PostDraft(BaseModel):
    title: str = Field(min_length=10, max_length=150)
    summary: str = Field(min_length=20, max_length=320, description="Одно предложение для недельного дайджеста")
    what_happened: str = Field(min_length=40, max_length=1300)
    why_important: str = Field(min_length=60, max_length=1500)
    key_details: list[str] = Field(min_length=1, max_length=6)
    impact_users: str = Field(default="", max_length=500)
    impact_developers: str = Field(default="", max_length=500)
    impact_business: str = Field(default="", max_length=500)
    outlook: str = Field(default="", max_length=600, description="Прогнозы/предположения; не факты")
    rumor: bool = False
    tags: list[str] = Field(min_length=1, max_length=5)

    @field_validator("title", "summary", "what_happened", "why_important", "outlook")
    @classmethod
    def _russian(cls, v, info):
        return _ru(v, info.field_name)

    @field_validator("impact_users", "impact_developers", "impact_business")
    @classmethod
    def _russian_opt(cls, v, info):
        return _ru(v, info.field_name)

    @field_validator("key_details")
    @classmethod
    def _details(cls, values):
        out = []
        for v in values:
            v = _ru(v, "key_details")
            if len(v) > 320:
                raise ValueError("key_details: each item ≤ 320 characters")
            if v:
                out.append(v)
        if not out:
            raise ValueError("key_details: at least one item")
        return out

    @field_validator("tags")
    @classmethod
    def _tags(cls, values):
        clean = []
        for t in values:
            t = re.sub(r"[\s\-]+", "_", t.strip().lstrip("#"))
            if not _TAG.fullmatch(t):
                raise ValueError(f"tags: invalid tag {t!r} (letters, digits, underscore only)")
            if t.casefold() not in {c.casefold() for c in clean}:
                clean.append(t)
        return clean[:5]

    @model_validator(mode="after")
    def _why_not_title(self):
        if fuzz.token_set_ratio(self.title.casefold(), self.why_important.casefold()) > 85:
            raise ValueError("why_important must explain consequences, not repeat the title")
        return self
