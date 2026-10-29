"""
Bilingual text (English + zh-Hant-TW) for every user-facing surface.

    locales/en.toml, locales/zh-Hant.toml   one key per string, same keys in both
    t(lang, key, **params)                  look up and format one string
    Translator(lang)                        the same, bound to one language
    Msg                                     a `str` (its English text) that remembers its key,
                                            so pure layers can emit phrases the delivery
                                            layers re-render in the reader's language

Framework-free: imported by `risk`, `alerts`, `core`, Telegram and the web app alike.
"""

from __future__ import annotations

import logging
import string
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

EN = "en"
ZH = "zh-Hant"
LANGUAGES = (EN, ZH)
DEFAULT = EN
LANGUAGE_NAMES = {EN: "English", ZH: "繁體中文"}
TELEGRAM_CODES = {EN: None, ZH: "zh"}

LOCALES = Path(__file__).resolve().parent / "locales"


def normalize(code: Optional[str]) -> str:
    """Any language tag → a supported language: `zh`, `zh-TW`, `zh-Hant-HK`, `zh_CN` … → zh-Hant; else English."""
    tag = str(code or "").strip().lower().replace("_", "-")
    return ZH if tag == "zh" or tag.startswith("zh-") else EN


def is_zh(lang: Optional[str]) -> bool:
    return normalize(lang) == ZH


def _flatten(table: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, name + "."))
        else:
            out[name] = str(value)
    return out


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    with open(LOCALES / f"{normalize(lang)}.toml", "rb") as f:
        return _flatten(tomllib.load(f))


@lru_cache(maxsize=None)
def web_catalog(lang: str) -> dict[str, str]:
    """The dashboard's `web.*` strings with the prefix stripped (English fills any gap)."""
    merged = {**catalog(EN), **catalog(lang)}
    return {k[4:]: v for k, v in merged.items() if k.startswith("web.")}


def has(key: str, lang: str = EN) -> bool:
    return key in catalog(lang)


def placeholders(template: str) -> set[str]:
    return {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(template) if name}


def t(lang: Optional[str], key: str, **params: Any) -> str:
    """`key` in `lang` (falling back to English, then to the key itself), formatted with `params`."""
    lang = normalize(lang)
    template = catalog(lang).get(key)
    if template is None:
        template = catalog(EN).get(key)
        if template is None:
            logger.warning("Missing i18n key %r", key)
            return key
    if not params:
        return template
    try:
        return template.format(**{k: tr(lang, v) for k, v in params.items()})
    except (KeyError, IndexError, ValueError) as e:
        logger.warning("Bad i18n params for %r: %s", key, type(e).__name__)
        return template


class Msg(str):
    """
    A phrase from a pure layer: behaves exactly like its English text
    (comparisons, JSON, joins), and `tr(lang, msg)` re-renders it from its
    key and params in any language.
    """

    key: str
    params: dict

    def __new__(cls, key: str, **params: Any) -> "Msg":
        obj = super().__new__(cls, t(EN, key, **params))
        obj.key = key
        obj.params = params
        return obj

    def __reduce__(self):
        return (_rebuild, (self.key, self.params))

    def __deepcopy__(self, memo) -> "Msg":
        return self

    def __copy__(self) -> "Msg":
        return self


def _rebuild(key: str, params: dict) -> Msg:
    return Msg(key, **params)


def tr(lang: Optional[str], value: Any) -> Any:
    """Render a `Msg` (or a list of them) in `lang`; anything else is returned unchanged."""
    if isinstance(value, Msg):
        return t(lang, value.key, **value.params)
    if isinstance(value, list):
        return [tr(lang, v) for v in value]
    if isinstance(value, tuple):
        return tuple(tr(lang, v) for v in value)
    return value


def join(lang: Optional[str], values: Iterable[Any], sep: Optional[str] = None) -> str:
    sep = sep if sep is not None else t(lang, "common.list_sep")
    return sep.join(str(tr(lang, v)) for v in values)


class Translator:
    """`t` bound to one language: `T("menu.title")`, `T.tr(msg)`, `T.lang`."""

    def __init__(self, lang: Optional[str] = None):
        self.lang = normalize(lang)

    def __call__(self, key: str, **params: Any) -> str:
        return t(self.lang, key, **params)

    def tr(self, value: Any) -> Any:
        return tr(self.lang, value)

    def join(self, values: Iterable[Any], sep: Optional[str] = None) -> str:
        return join(self.lang, values, sep)

    @property
    def is_zh(self) -> bool:
        return self.lang == ZH

    def __repr__(self) -> str:
        return f"Translator({self.lang!r})"


def muse_language(lang: Optional[str]) -> str:
    """How the Muse prompts name the reply language."""
    return t(lang, "muse.reply_language")


__all__ = [
    "DEFAULT", "EN", "LANGUAGES", "LANGUAGE_NAMES", "Msg", "TELEGRAM_CODES", "Translator", "ZH",
    "catalog", "has", "is_zh", "join", "muse_language", "normalize", "placeholders", "t", "tr",
]
