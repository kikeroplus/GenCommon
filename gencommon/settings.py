"""LLM の接続 (読み取り・エンハンサー・PromptWordCrafter の翻訳で同じサーバーを使う。モデルは別々に選ぶ)。

3 つのアプリで共有する (%APPDATA%\\GenCommon)。
"""

from typing import Optional

from . import store

DEFAULT_LLM_URL = "http://192.168.11.6:1234/v1"
DEFAULT_LLM_MODEL = "qwen2.5-14b-instruct-uncensored"
DEFAULT_MAX_SIDE = 1024  # 読み取りで画像を送る前に縮める大きさ (長い辺のピクセル数)


def get(name: str, default: str = "") -> str:
    value = store.load(name)
    return default if value is None else value


def put(name: str, value) -> None:
    store.save(name, str(value))


def get_int(name: str, default: int, low: Optional[int] = None, high: Optional[int] = None) -> int:
    try:
        value = int(float(get(name)))
    except ValueError:
        return default
    if (low is not None and value < low) or (high is not None and value > high):
        return default
    return value


def llm_url() -> str:
    return get("llm_url.txt") or DEFAULT_LLM_URL


def set_llm_url(url: str) -> None:
    put("llm_url.txt", url.strip().rstrip("/"))


def reader_model() -> str:
    return get("reader_model.txt") or enhance_model_raw() or DEFAULT_LLM_MODEL


def set_reader_model(model: str) -> None:
    put("reader_model.txt", model.strip())


def enhance_model_raw() -> str:
    return get("enhance_model.txt")


def enhance_model() -> str:
    return get("enhance_model.txt") or get("reader_model.txt") or DEFAULT_LLM_MODEL


def set_enhance_model(model: str) -> None:
    put("enhance_model.txt", model.strip())


def max_side() -> int:
    return get_int("reader_max_side.txt", DEFAULT_MAX_SIDE, 256, 4096)


def set_max_side(value: int) -> None:
    put("reader_max_side.txt", value)
