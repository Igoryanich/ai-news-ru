"""Pure text/URL helpers (no I/O)."""

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "ocid", "sr_share", "igshid"}


def canonical_url(url: str) -> str:
    """Normalise a URL for exact-duplicate detection. Raises ValueError for non-http(s)."""
    p = urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("unsupported url")
    host = p.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in {"m.youtube.com", "youtu.be"}:
        host = "youtube.com"
    netloc = host if not p.port or p.port in (80, 443) else f"{host}:{p.port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def html_to_text(value: str | None, limit: int = 20000) -> str:
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        text = value
    else:
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def clip(text: str, limit: int) -> str:
    """Cut at a word/sentence boundary; never mid-word."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? ", "; ", ", ", " "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[: pos + (1 if sep != " " else 0)].rstrip() + "…"
    return cut.rstrip() + "…"


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Cheap ru/en detection — enough to label items; the LLM handles translation."""
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if cyr == 0 and lat == 0:
        return "und"
    return "ru" if cyr >= lat else "en"


def has_cyrillic(text: str) -> bool:
    return bool(_CYR.search(text or ""))


def normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[‐-―]", "-", text)
    return " ".join(re.findall(r"[\w.+-]+", text))


# Model/product version tokens: "gpt-5", "gemini 3.8", "llama-4", "v2.1", "o3", "r2", "3.5".
_VERSION = re.compile(
    r"\b(?:[a-zа-я]+[- ]?)?v?\d+(?:\.\d+)+[a-z]?\b"  # 3.8, gemini-3.8, v2.1
    r"|\b[a-zа-я]+-\d+[a-z]?\b"  # gpt-5, llama-4, qwen-3
    r"|\b[a-z]\d{1,2}\b",  # o3, r2
    re.I,
)
_VERSION_NUM = re.compile(r"\d+(?:\.\d+)*[a-z]?", re.I)


def version_tokens(text: str) -> set[str]:
    """Numeric version signatures, language-independent ("GPT-5.1" and "GPT‑5.1" → {"5.1"})."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[‐-―]", "-", text)
    out: set[str] = set()
    for m in _VERSION.finditer(text):
        token = m.group(0)
        # Skip years and plain large counts (e.g. "2026", "100").
        nums = _VERSION_NUM.findall(token)
        for n in nums:
            if re.fullmatch(r"(19|20)\d\d", n):
                continue
            out.add(n.lower())
    return out


_RUMOR = re.compile(
    r"\b(rumou?rs?|reportedly|allegedly|leak(?:ed|s)?|unconfirmed|sources say|according to (?:people|sources)"
    r"|слух\w*|утечк\w*|якобы|по данным источников|неподтвержд\w*|инсайд\w*)\b",
    re.I,
)


def rumor_hint(text: str) -> bool:
    return bool(_RUMOR.search(text or ""))


_AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|machine learning|ml|llm|llms|gpt|chatgpt|openai|anthropic|claude"
    r"|gemini|deepmind|copilot|mistral|llama|hugging ?face|nvidia|neural|transformer|diffusion|agent(?:s|ic)?"
    r"|model(?:s)?|inference|fine-?tun\w*|rag|embedding\w*|robot\w*|xai|grok|deepseek|qwen|perplexity|cursor"
    r"|нейросет\w*|ии|искусственн\w+ интеллект\w*|машинн\w+ обучен\w*|модел\w*|агент\w*|чат-?бот\w*)\b",
    re.I,
)


def looks_ai_related(text: str) -> bool:
    return bool(_AI_WORDS.search(text or ""))


_PV_STOP = {"top", "part", "day", "days", "version", "update", "model", "models", "the", "and", "for", "with",
            "over", "from", "than", "only", "under", "about", "топ", "версия", "модель", "модели", "обновление",
            "q1", "q2", "q3", "q4", "covid", "windows", "ios", "android"}
_PV_DOTTED = re.compile(r"([a-zа-яё][a-zа-яё]{2,})[\s-]+v?(\d+\.\d+(?:\.\d+)?)\b", re.I)
_PV_HYPHEN = re.compile(r"\b([a-zа-яё][a-zа-яё]{1,})-(\d{1,2}(?:\.\d+)?)\b", re.I)


def product_versions(text: str) -> set[tuple[str, str]]:
    """Product+version signatures: "Claude Sonnet 5.5" → {("sonnet", "5.5")}, "GPT‑5.1" → {("gpt", "5.1")}.
    Two titles sharing one describe the same release (language-independent: names stay in Latin)."""
    text = re.sub(r"[‐-―]", "-", unicodedata.normalize("NFKC", text)).casefold()
    out = set()
    for rx in (_PV_DOTTED, _PV_HYPHEN):
        for word, ver in rx.findall(text):
            if word not in _PV_STOP:
                out.add((word, ver))
    return out
