"""ComfyUI で画像・動画を生成する (PromptWordCrafter と ImageMimicArt で共通)。

プロンプト (盛ったときは盛ったプロンプト) をワークフローのプロンプトの場所に入れ、
ベースプロンプトを改行をはさんで後ろに付ける。元画像のあるアプリ (ImageMimicArt) では、
ワークフローに LoadImage があれば元画像も送る。出来たファイルの置き場所と名前はアプリが決める
(app.Profile.output_path)。

J-Rate の ComfyVideo.cs と同じ決まりごと (generation-standard.md) で、
設定・記録・ワークフローの書き換え・ComfyUI とのやりとりを受け持つ。
設定の置き場所は store.py (共有するものは %APPDATA%\\GenCommon、ほかはアプリごと)。
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import app, inserts, store

DEFAULT_URL = "http://127.0.0.1:8188"
PRESET_COUNT = 10
WORKFLOW_COUNT = 100
PROMPT_COUNT = 100

VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov", ".mkv", ".gif", ".webp")
IMAGE_EXTS = (".png", ".jpg", ".jpeg")

# 3 つのアプリのどれでコピーしたパラメーターも貼り付けられるようにする
PARAMS_HEADERS = ("[J-Rate 生成パラメーター]", "[PromptWordCrafter 生成パラメーター]", "[ImageMimicArt 生成パラメーター]")


def params_header() -> str:
    return f"[{app.profile().name} 生成パラメーター]"


EXPORT_KIND = "comfy-settings"

_rng = random.Random()
_history_lock = threading.Lock()
_save_lock = threading.Lock()  # 出来たファイルの名前を決めて保存する間


class ComfyError(Exception):
    """利用者に見せるエラー (文は日本語)。"""


class ComfyConnectionError(ComfyError):
    """ComfyUI に接続できない。"""


class GenerationCancelled(Exception):
    pass


class GenerationHeld(Exception):
    """一時停止中なので、依頼し直さずに待機に戻す。sent は ComfyUI 側に残っているかもしれないとき True。"""

    def __init__(self, sent: bool):
        super().__init__()
        self.sent = sent


# ----------------------------------------------------------------------
# プリセット・セット・1 回の生成の設定
# ----------------------------------------------------------------------
@dataclass
class Preset:
    """名前と値。値は、接続先なら URL、ワークフローなら JSON のパス。"""

    name: str = ""
    value: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.value

    def label(self, index: int) -> str:
        return self._label(index, self.value)

    def file_label(self, index: int) -> str:
        """ワークフロー用: パスの代わりにファイル名を出す (名前がファイル名と同じなら出さない)。"""
        name = file_name(self.value)
        if self.name and self.name == Path(name).stem:
            name = ""
        return self._label(index, name)

    def clear(self):
        self.name = ""
        self.value = ""

    def _label(self, index: int, detail: str) -> str:
        if self.name:
            text = f"{self.name}  ({detail})" if detail else self.name
        else:
            text = detail or "(未登録)"
        return f"{index + 1}: {text}"


@dataclass
class ComfySet:
    """接続先・ワークフロー・長さ・読み取り・エンハンサーをまとめて呼び出すセット。中身は値で覚える。

    プロンプトはファイル (テキストの本文・説明プロンプト) から取るので、このアプリでは prompt を空で保存する
    (J-Rate と同じ形式のファイルなので、項目は残しておく)。
    読み取りプリセットとエンハンサーは共有の登録なので、名前で覚える (generation-standard 2 章)。
    None はその項目を持たない (前の形式・持たないアプリで保存した) もので、選んでもその欄は変えない。
    """

    name: str = ""
    url: str = ""
    workflow: str = ""
    prompt: str = ""
    seconds: str = ""
    prompt_preset: int = -1  # 保存したときに選んでいたプロンプトのプリセット (0 始まり。なければ -1)
    reader: Optional[str] = None  # 読み取りプリセットの名前 (空 = なし)
    reread: Optional[bool] = None  # 読み取り済みでも読み直す
    enhancer: Optional[str] = None  # エンハンサーの名前 (空 = なし)
    inserts: Optional[list] = None  # 差し込みの欄の形 (inserts.py。フォルダは値で覚える)

    @property
    def is_empty(self) -> bool:
        return not self.url and not self.workflow

    def label(self, index: int) -> str:
        return f"{index + 1}: " + ("(未登録)" if self.is_empty else self.name or "(名前なし)")


@dataclass
class GenRequest:
    connection_name: str = ""
    url: str = ""
    workflow_name: str = ""
    workflow_path: str = ""
    prompt: Optional[str] = None  # 送る本文 (読み取るアプリでは説明プロンプト)。None = ワークフローのまま / まだ読み取っていない
    seconds: Optional[float] = None  # 同上
    base_prompt: str = ""  # 生成を始めたときのベースプロンプト (prompt のあとに改行をはさんで送る)
    # AI で盛ってから送るときの、登録した時点のエンハンサーの写し
    # (name・system・template・temperature・url・model)。記録から読んだものは name だけ
    enhancer: Optional[dict] = None
    enhanced_prompt: Optional[str] = None  # 盛ったプロンプト (ジョブの番が来たときに決まる)
    # 読み取るときの、登録した時点の読み取りプリセットの写し
    # (name・system・template・temperature・url・model・max_side)。記録から読んだものは name だけ
    reader: Optional[dict] = None
    output_dir: str = ""  # 出力フォルダ (出力フォルダを持つアプリで、生成を始めたときのもの)
    # 差し込み (inserts.py)。ジョブ・記録では使った枠ごとの {slot, name, folder, before, file, text}。
    # 生成の画面の入力中の内容 (_fields) では欄の形 {slot, name, folder, before, use}。None = 持たない
    inserts: Optional[list] = None

    @property
    def reader_name(self) -> str:
        return str((self.reader or {}).get("name", ""))

    @property
    def enhancer_name(self) -> str:
        return str((self.enhancer or {}).get("name", ""))

    @property
    def source_prompt(self) -> Optional[str]:
        """エンハンサーに渡す文: 元のプロンプト + 盛る前に入れる差し込み (改行でつなぐ)。"""
        if self.prompt is None:
            return None
        return inserts.join(self.prompt, *inserts.texts(self.inserts, True))

    @property
    def full_prompt(self) -> Optional[str]:
        """ComfyUI に送る文: (盛ったプロンプト か 元のプロンプト + 盛る前の差し込み) + 盛ったあとの差し込み + ベース。"""
        if self.prompt is None:
            return None
        p = self.source_prompt if self.enhanced_prompt is None else self.enhanced_prompt
        return inserts.join(p, *inserts.texts(self.inserts, False), self.base_prompt)

    def copy(self, **changes) -> "GenRequest":
        data = dict(self.__dict__)
        data.update(changes)
        return GenRequest(**data)


@dataclass
class GenRecord:
    output: str = ""
    source: str = ""
    time: str = ""
    request: GenRequest = field(default_factory=GenRequest)


def file_name(path: str) -> str:
    """他の PC のパスでも区切りが \\ と / のどちらでもファイル名を取り出す。"""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def format_seconds(seconds: float) -> str:
    return str(int(seconds)) if seconds == int(seconds) else repr(seconds)


# ----------------------------------------------------------------------
# パラメーターのコピー / 貼り付け (人が読める文章)
# ----------------------------------------------------------------------
def params_to_text(r: GenRequest) -> str:
    lines = [
        params_header(),
        f"接続先: {r.connection_name}",
        f"URL: {r.url}",
        f"ワークフロー: {r.workflow_name}",
        f"ワークフローのパス: {r.workflow_path}",
    ]
    if r.seconds is not None:
        lines.append(f"長さ: {format_seconds(r.seconds)}")
    text = "\r\n".join(lines) + "\r\n"
    if r.prompt is not None:
        text += "プロンプト:\r\n" + r.prompt.strip()
    return text


def parse_params(text: str) -> Optional[GenRequest]:
    """目印のない文字列なら None。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() not in PARAMS_HEADERS:
        return None
    r = GenRequest()
    for j in range(i + 1, len(lines)):
        line = lines[j]
        if line.strip() == "プロンプト:":
            r.prompt = "\r\n".join(lines[j + 1:]).strip()
            break
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key == "接続先":
            r.connection_name = value
        elif key == "URL":
            r.url = value
        elif key == "ワークフロー":
            r.workflow_name = value
        elif key == "ワークフローのパス":
            r.workflow_path = value
        elif key == "長さ":
            try:
                r.seconds = float(value)
            except ValueError:
                pass
    return r


# ----------------------------------------------------------------------
# 設定ファイル (置き場所は store.py。共有するものは %APPDATA%\GenCommon)
# ----------------------------------------------------------------------
file_path = store.file_path
load = store.load
save = store.save


def _one_line(text: str) -> str:
    return (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _load_presets(name: str, legacy: str, legacy_default: str, count: int) -> list[Preset]:
    presets = [Preset() for _ in range(count)]
    text = load(name)
    if text is None:
        old = load(legacy)
        presets[0].value = old.strip().rstrip("/") if old and old.strip() else legacy_default
        return presets
    for line in text.split("\n"):
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 3:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            continue
        if 1 <= n <= count:
            presets[n - 1].name = parts[1].strip()
            presets[n - 1].value = parts[2].strip().rstrip("/")
    return presets


def _save_presets(name: str, presets: list[Preset], count: int) -> None:
    lines = [
        f"{i + 1}\t{_one_line(p.name)}\t{_one_line(p.value).rstrip('/')}\r\n"
        for i, p in enumerate(presets[:count])
    ]
    save(name, "".join(lines))


def _load_index(name: str, count: int) -> int:
    try:
        n = int(load(name) or "")
    except ValueError:
        return 0
    return n - 1 if 1 <= n <= count else 0


def _load_optional_index(name: str, count: int) -> int:
    try:
        n = int(load(name) or "")
    except ValueError:
        return -1
    return n - 1 if 1 <= n <= count else -1


def update_preset(file: str, index: int, preset: Optional[Preset]) -> list[Preset]:
    """共有の一覧 (接続先 / ワークフロー) の 1 枠だけを書き換える。preset が None なら空にする。

    ほかのアプリが同時に変えていても消さないように、ファイルを読み直してからその枠だけを変える。
    戻り値は書いたあとの一覧。
    """
    items = connections() if file == store.CONNECTIONS_FILE else workflows()
    items[index] = Preset() if preset is None else Preset(preset.name, preset.value)
    _save_presets(file, items, len(items))
    return items


def rearrange_presets(file: str, listed: list[int], result) -> tuple[list[Preset], dict[int, int]]:
    """共有の一覧 (接続先 / ワークフロー) に「まとめて編集」の結果 (bulk_edit.Result) を当てはめる。

    読み直してから書き、すべてのアプリの使用中の番号も並べ替えに合わせる。
    削除した接続先は、そのワークフローのフォルダも消す (同じ URL がほかの枠に残っていれば残す)。
    戻り値は (書いたあとの一覧, 元の番号 → 新しい番号)。
    """
    from .bulk_edit import apply  # 画面の部品と同じファイルにあるので、使うときに読む

    current = connections() if file == store.CONNECTIONS_FILE else workflows()
    deleted_urls = [current[i].value for i in result.deleted if 0 <= i < len(current)]
    items, mapping = apply(current, listed, result, Preset, lambda p: p.is_empty and not p.name)
    _save_presets(file, items, len(items))
    store.remap_indexes(file, mapping)
    if file == store.CONNECTIONS_FILE and deleted_urls:
        folders = connection_folders()
        remaining = {_url_key(p.value) for p in items if not p.is_empty}
        for url in deleted_urls:
            if _url_key(url) not in remaining:
                folders.pop(_url_key(url), None)
        save(store.CONNECTION_FOLDERS_FILE, "".join(f"{u}\t{f}\r\n" for u, f in folders.items()))
    return items, mapping


def swap_presets(file: str, a: int, b: int) -> list[Preset]:
    """共有の一覧の a 番と b 番を入れ替え、すべてのアプリの使用中の番号も合わせる。"""
    items = connections() if file == store.CONNECTIONS_FILE else workflows()
    items[a], items[b] = items[b], items[a]
    _save_presets(file, items, len(items))
    store.swap_indexes(file, a, b)
    return items


def connections() -> list[Preset]:
    return _load_presets("comfy_presets.txt", "comfy_url.txt", DEFAULT_URL, PRESET_COUNT)


def set_connections(presets: list[Preset]) -> None:
    _save_presets("comfy_presets.txt", presets, PRESET_COUNT)


def active_connection() -> int:
    return _load_index("comfy_preset.txt", PRESET_COUNT)


def set_active_connection(index: int) -> None:
    save("comfy_preset.txt", str(index + 1))


def workflows() -> list[Preset]:
    return _load_presets("comfy_workflows.txt", "comfy_workflow.txt", "", WORKFLOW_COUNT)


def set_workflows(presets: list[Preset]) -> None:
    _save_presets("comfy_workflows.txt", presets, WORKFLOW_COUNT)


def active_workflow() -> int:
    return _load_index("comfy_workflow_preset.txt", WORKFLOW_COUNT)


def set_active_workflow(index: int) -> None:
    save("comfy_workflow_preset.txt", str(index + 1))


# ----------------------------------------------------------------------
# 接続先ごとのワークフローのフォルダ (共有。comfy_connection_folders.txt に「URL Tab フォルダ」)
# ワークフローがどの台用かは、置いてあるフォルダで見分ける。どの接続先のフォルダにもないものは「共通」
# ----------------------------------------------------------------------
def _url_key(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _folder_key(path: str) -> str:
    text = (path or "").strip().strip('"')
    return os.path.normcase(os.path.normpath(text)) if text else ""


def connection_folders() -> dict[str, str]:
    """{接続先の URL (小文字): ワークフローのフォルダ}。"""
    folders: dict[str, str] = {}
    for line in (load(store.CONNECTION_FOLDERS_FILE) or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            folders[_url_key(parts[0])] = parts[1].strip()
    return folders


def set_connection_folder(url: str, folder: str, old_url: str = "") -> None:
    """接続先のワークフローのフォルダを書く (空なら消す)。old_url は URL を書き換えたときの前の URL。

    ほかのアプリが同時に変えていても消さないよう、読み直してからその行だけを変える。
    """
    folders = connection_folders()
    if old_url and _url_key(old_url) != _url_key(url):
        folders.pop(_url_key(old_url), None)
    key = _url_key(url)
    folder = _one_line(folder).strip('"')
    if key and folder:
        folders[key] = folder
    elif key:
        folders.pop(key, None)
    save(store.CONNECTION_FOLDERS_FILE, "".join(f"{u}\t{f}\r\n" for u, f in folders.items()))


def _in_folder(path: str, folder: str) -> bool:
    p, f = _folder_key(path), _folder_key(folder)
    return bool(p and f) and p.startswith(f.rstrip("\\/") + os.sep)


def workflow_machines(path: str, conns: Optional[list[Preset]] = None,
                      folders: Optional[dict[str, str]] = None) -> list[Preset]:
    """ワークフローを置いてあるフォルダが、ワークフローのフォルダになっている接続先 (共通のものは空)。"""
    conns = connections() if conns is None else conns
    folders = connection_folders() if folders is None else folders
    return [c for c in conns if not c.is_empty and _in_folder(path, folders.get(_url_key(c.value), ""))]


def machine_name(path: str, conns: Optional[list[Preset]] = None,
                 folders: Optional[dict[str, str]] = None) -> str:
    """ワークフローの台の名前 (接続先の名前。名前がなければフォルダ名。共通なら空)。"""
    folders = connection_folders() if folders is None else folders
    names = []
    for c in workflow_machines(path, conns, folders):
        name = c.name or file_name(folders.get(_url_key(c.value), "").rstrip("\\/"))
        if name and name not in names:
            names.append(name)
    return " / ".join(names)


def machine_tag(name: str, machine: str) -> str:
    """一覧に出す名前の後ろに付ける台の名前 (名前にもう入っていれば付けない)。"""
    if not machine or machine.strip("[]［］").lower() in name.lower():
        return ""
    return f"  ［{machine.strip('[]［］')}］"


def machine_conflict(url: str, path: str, conns: Optional[list[Preset]] = None,
                     folders: Optional[dict[str, str]] = None) -> str:
    """ワークフローが別の台用なら、その台の名前 (合っている・共通なら空)。"""
    machines = workflow_machines(path, conns, folders)
    if not machines or any(_url_key(c.value) == _url_key(url) for c in machines):
        return ""
    return machine_name(path, conns, folders)


def unregistered_workflows(folder: str) -> list[Path]:
    """フォルダにある .json のうち、まだワークフローに登録していないもの (名前順)。読めなければ空。"""
    registered = {_folder_key(p.value) for p in workflows() if not p.is_empty}
    try:
        found = sorted((p for p in Path(folder).glob("*.json") if p.is_file()), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [p for p in found if _folder_key(str(p)) not in registered]


def register_workflows(paths: list[str]) -> tuple[int, int]:
    """空いている枠に、ファイル名 (拡張子なし) を名前にして登録する。戻り値は (登録した数, 枠が足りず登録できなかった数)。"""
    items = workflows()
    added = 0
    for path in paths:
        slot = next((i for i, p in enumerate(items) if p.is_empty and not p.name), -1)
        if slot < 0:
            break
        items = update_preset(store.WORKFLOWS_FILE, slot, Preset(Path(path).stem.strip(), path))
        added += 1
    return added, len(paths) - added


def active_workflow_path() -> str:
    return workflows()[active_workflow()].value


def base_prompt_video() -> str:
    return load("comfy_base_prompt_video.txt") or load("comfy_base_prompt.txt") or ""


def set_base_prompt_video(text: str) -> None:
    save("comfy_base_prompt_video.txt", (text or "").strip())


def base_prompt_image() -> str:
    return load("comfy_base_prompt_image.txt") or load("comfy_base_prompt.txt") or ""


def set_base_prompt_image(text: str) -> None:
    save("comfy_base_prompt_image.txt", (text or "").strip())


def base_prompt_for(video: bool) -> str:
    return base_prompt_video() if video else base_prompt_image()


def use_base_prompt() -> bool:
    """ベースプロンプトを付けて送るか (生成の画面のチェック。既定はオン)。"""
    return load("comfy_use_base_prompt.txt") != "0"


def set_use_base_prompt(use: bool) -> None:
    save("comfy_use_base_prompt.txt", "1" if use else "0")


def last_seconds() -> str:
    return load("comfy_seconds.txt") or ""


def set_last_seconds(text: str) -> None:
    save("comfy_seconds.txt", (text or "").strip())


def output_folder() -> str:
    """出力フォルダ (出力フォルダを持つアプリ。空なら、まだ決めていない)。"""
    return load("output_folder.txt") or ""


def set_output_folder(path: str) -> None:
    save("output_folder.txt", (path or "").strip())


def open_result() -> bool:
    """生成が終わったら、出来たファイルを表示するか (既定はオン。まとめて生成のときは表示しない)。"""
    return load("comfy_jump.txt") != "0"


def set_open_result(value: bool) -> None:
    save("comfy_jump.txt", "1" if value else "0")


def sets() -> list[ComfySet]:
    result = [ComfySet() for _ in range(PRESET_COUNT)]
    try:
        data = json.loads(load("comfy_sets.txt") or "[]")
    except ValueError:
        data = []
    if not isinstance(data, list):
        data = []
    for i, item in enumerate(data[:PRESET_COUNT]):
        if not isinstance(item, dict):
            continue
        s = result[i]
        s.name = _str(item, "name")
        s.url = _str(item, "url")
        s.workflow = _str(item, "workflow")
        s.prompt = _str(item, "prompt")
        s.seconds = _str(item, "seconds")
        try:
            pp = int(_str(item, "promptPreset"))
        except ValueError:
            pp = 0
        s.prompt_preset = pp - 1 if 1 <= pp <= PROMPT_COUNT else -1
        if "reader" in item:
            s.reader = _str(item, "reader")
        if "reread" in item:
            s.reread = bool(item.get("reread"))
        if "enhancer" in item:
            s.enhancer = _str(item, "enhancer")
        if isinstance(item.get("inserts"), list):
            s.inserts = [d for d in item["inserts"] if isinstance(d, dict)]
    return result


def set_sets(items: list[ComfySet]) -> None:
    data = []
    for s in items:
        item = {
            "name": s.name,
            "url": s.url,
            "workflow": s.workflow,
            "prompt": s.prompt,
            "seconds": s.seconds,
            "promptPreset": s.prompt_preset + 1,
        }
        if s.reader is not None:
            item["reader"] = s.reader
        if s.reread is not None:
            item["reread"] = s.reread
        if s.enhancer is not None:
            item["enhancer"] = s.enhancer
        if s.inserts is not None:
            item["inserts"] = s.inserts
        data.append(item)
    save("comfy_sets.txt", json.dumps(data, ensure_ascii=False))


def last_set() -> int:
    return _load_optional_index("comfy_set.txt", PRESET_COUNT)


def set_last_set(index: int) -> None:
    save("comfy_set.txt", str(index + 1))


def _str(data: dict, key: str) -> str:
    value = data.get(key)
    return "" if value is None else str(value)


# ----------------------------------------------------------------------
# エクスポート / インポート (J-Rate と同じ形式なので、互いに読み込める)
# ----------------------------------------------------------------------
def import_backup_path() -> Path:
    return file_path("backup_before_import.json")


# 共有するファイルのうち comfy_*.txt 以外のもの (エンハンサー・読み取りプリセット・LLM の接続)。
# 前の形式のエクスポートには入っていないので、インポートでは入っているときだけ置き換える
_EXTRA_SETTING_FILES = (store.ENHANCER_FILE, store.READER_FILE) + store.LLM_FILES


def _is_setting_file(name: str) -> bool:
    if name in _EXTRA_SETTING_FILES:
        return True
    if not name.startswith("comfy_") or not name.endswith(".txt") or name == "comfy_history.txt":
        return False
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789_." for c in name)


def _setting_files() -> list[Path]:
    """今の設定のファイル (共有のものと、このアプリのもの)。"""
    found = []
    for folder, shared in ((store.shared_dir(), True), (store.APP_DIR, False)):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            # 引き継ぐ前の古いファイル (共有するものの名前でアプリのフォルダにあるもの) は使っていないので除く
            if p.is_file() and _is_setting_file(p.name) and store.is_shared(p.name) == shared:
                found.append(p)
    return found


def export_settings(path: str) -> str:
    files = {p.name: p.read_text(encoding="utf-8-sig") for p in _setting_files()}
    paths = [p.value for p in workflows()] + [s.workflow for s in sets()]
    workflow_items = []
    seen = set()
    missing = 0
    for wf in paths:
        key = wf.lower()
        if not wf or key in seen:
            continue
        seen.add(key)
        try:
            content = Path(wf).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            missing += 1
            continue
        workflow_items.append({"path": wf, "content": content})

    root = {
        "app": app.profile().name,
        "kind": EXPORT_KIND,
        "version": 1,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": files,
        "workflows": workflow_items,
    }
    Path(path).write_text(json.dumps(root, ensure_ascii=False), encoding="utf-8")
    note = f"。{missing} 個は読めなかったので含めていません" if missing else ""
    return f"生成の設定をエクスポートしました (ワークフローの JSON {len(workflow_items)} 個を含む{note}): {path}"


def import_settings(path: str) -> str:
    """今の設定をすべて置き換える。読めないファイルのときは ComfyError (設定は変えない)。"""
    try:
        root = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ComfyError(f"ファイルを読めません。\n{exc}") from exc
    files = root.get("files") if isinstance(root, dict) else None
    if not isinstance(files, dict) or _str(root, "kind") != EXPORT_KIND:
        raise ComfyError("生成の設定をエクスポートしたファイルではありません。")
    if not store.online():
        raise ComfyError(store.offline_message())

    try:
        export_settings(str(import_backup_path()))
    except OSError:
        pass

    # comfy_*.txt はすべて置き換える。エンハンサーなど、前の形式に入っていないものは入っているときだけ
    for p in _setting_files():
        if p.name not in _EXTRA_SETTING_FILES or p.name in files:
            p.unlink()
    for name, content in files.items():
        if _is_setting_file(name) and content is not None and name != store.APPS_FILE:
            store.write_text(file_path(name), str(content))

    # この PC にないワークフローは書き出して、登録してあるパスを付け替える
    moved: dict[str, str] = {}
    for item in root.get("workflows") or []:
        if not isinstance(item, dict):
            continue
        old = _str(item, "path")
        if not old or old.lower() in moved:
            continue
        try:
            exists = Path(old).is_file()
        except OSError:
            exists = False
        if not exists:
            moved[old.lower()] = _restore_workflow(old, _str(item, "content"))
    if moved:
        presets = workflows()
        for p in presets:
            p.value = moved.get(p.value.lower(), p.value)
        set_workflows(presets)
        items = sets()
        if any(s.workflow.lower() in moved for s in items):
            for s in items:
                s.workflow = moved.get(s.workflow.lower(), s.workflow)
            set_sets(items)

    message = "生成の設定をインポートしました。"
    if moved:
        message += (
            f"\n\nこの PC になかったワークフローの JSON {len(moved)} 個を次のフォルダに書き出しました。\n"
            f"{file_path('workflows')}"
        )
    return message


def _restore_workflow(old_path: str, content: str) -> str:
    name = file_name(old_path)
    if not name or any(c in name for c in '\\/:*?"<>|'):
        name = "workflow.json"
    folder = file_path("workflows")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 2
    while target.exists():
        try:
            if target.read_text(encoding="utf-8-sig") == content:
                return str(target)
        except (OSError, UnicodeDecodeError):
            pass
        target = folder / f"{stem} ({i}){suffix}"
        i += 1
    target.write_text(content, encoding="utf-8")
    return str(target)


# ----------------------------------------------------------------------
# 生成の記録 (%APPDATA%\(アプリ名)\comfy_history.txt、1 行に 1 件の JSON。アプリごと)
# ----------------------------------------------------------------------
def add_history(output: str, source: str, req: GenRequest) -> None:
    record = {
        "output": output,
        "source": source,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "connection": req.connection_name,
        "url": req.url,
        "workflow": req.workflow_name,
        "workflowPath": req.workflow_path,
        "prompt": req.prompt,
        "basePrompt": req.base_prompt,
        "seconds": req.seconds,
    }
    if req.reader is not None:
        record["reader"] = req.reader_name
    if req.enhancer is not None:
        # prompt は元のプロンプトのまま (「旧」の判定に使う)。実際に送った文は enhancedPrompt
        record["enhancer"] = req.enhancer_name
        record["enhancedPrompt"] = req.enhanced_prompt
    if req.inserts:
        # prompt には混ぜない (「旧」の判定に使うので)。使った枠ごとの名前・フォルダ・ファイル・中身
        record["inserts"] = req.inserts
    try:
        with _history_lock:
            store.APP_DIR.mkdir(parents=True, exist_ok=True)
            with file_path("comfy_history.txt").open("a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\r\n")
    except OSError:
        pass


def find_history_by_source(source: str) -> Optional[GenRecord]:
    """その元のファイルから生成したときの記録 (新しいものを優先)。

    同じパスの記録を探し、なければ同じファイル名の記録 (あとで別のフォルダへ移したもの) を使う。
    """
    name = Path(source).name.lower()
    target = os.path.normcase(os.path.abspath(source))
    by_name = None
    for record in reversed(read_history()):
        recorded = record.source
        if not recorded or name not in recorded.lower():
            continue
        if os.path.normcase(os.path.abspath(recorded)) == target:
            return record
        if by_name is None and file_name(recorded).lower() == name:
            by_name = record
    return by_name


# 記録は書き足す一方なので、前に読んだところから先だけを読む。
# ((パス, ファイルの番号), 読んだバイト数, 記録)。書き換えたとき (rename_in_history) は捨てる
_history_cache: tuple = (None, 0, [])


def read_history() -> list[GenRecord]:
    """すべての記録 (古い順)。

    ファイルを選ぶたびに呼ばれるので、前に読んだものに書き足された分だけを足して返す。
    返した記録は書き換えないこと (読むだけの呼び出し元で共有している)。
    """
    global _history_cache
    try:
        with _history_lock:
            path = file_path("comfy_history.txt")
            try:
                stat = path.stat()
            except FileNotFoundError:
                _history_cache = (None, 0, [])
                return []
            identity = (str(path), stat.st_ino)
            cached_identity, offset, records = _history_cache
            if cached_identity != identity or stat.st_size < offset:
                offset, records = 0, []  # 初めて読む・別のファイルに置き換わった・短くなった
            if stat.st_size == offset:
                return list(records)
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read()
    except OSError:
        return []
    end = data.rfind(b"\n") + 1  # 書きかけの最後の行は、次に読む
    if end == 0:
        return list(records)
    try:
        text = data[:end].decode("utf-8-sig" if offset == 0 else "utf-8")
    except UnicodeDecodeError:
        return list(records)
    records = list(records)
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(_to_record(item))
    _history_cache = (identity, offset + end, records)
    return list(records)


def rename_in_history(renames: dict[str, str], app_name: Optional[str] = None) -> int:
    """記録の元のファイルと出来たファイルのパスを付け替える (名前を変えたあとに呼ぶ)。

    renames は {旧パス: 新パス}。大文字小文字は区別しない。戻り値は書き換えた行の数。
    app_name を渡すと、別のアプリの記録 (%APPDATA%\\(app_name)\\comfy_history.txt) を書き換える
    (PromptWordCrafter で結果を移したとき、それを元画像にした ImageMimicArt の記録も付け替えるなど)。
    """
    global _history_cache
    if not renames:
        return 0
    table = {os.path.normcase(os.path.abspath(old)): new for old, new in renames.items()}

    def mapped(value: str) -> Optional[str]:
        if not value:
            return None
        return table.get(os.path.normcase(os.path.abspath(value)))

    changed = 0
    try:
        with _history_lock:
            other = app_name is not None and app_name != store.APP_NAME
            if other:
                path = store.APP_DIR.parent / app_name / "comfy_history.txt"
            else:
                path = file_path("comfy_history.txt")
            if not path.is_file():
                return 0
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            out = []
            for line in lines:
                try:
                    data = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if isinstance(data, dict):
                    hit = False
                    for key in ("source", "output"):
                        new = mapped(_str(data, key))
                        if new is not None:
                            data[key] = new
                            hit = True
                    if hit:
                        changed += 1
                        line = json.dumps(data, ensure_ascii=False)
                out.append(line)
            if changed:
                temp = path.with_name(path.name + ".tmp")
                temp.write_text("".join(x + "\r\n" for x in out), encoding="utf-8", newline="")
                temp.replace(path)
                if not other:
                    _history_cache = (None, 0, [])  # 書き換えたので、次は読み直す
    except (OSError, UnicodeDecodeError):
        return 0
    return changed


def _to_record(data: dict) -> GenRecord:
    req = GenRequest(
        connection_name=_str(data, "connection"),
        url=_str(data, "url"),
        workflow_name=_str(data, "workflow"),
        workflow_path=_str(data, "workflowPath"),
        prompt=None if data.get("prompt") is None else str(data["prompt"]),
        base_prompt=_str(data, "basePrompt"),
    )
    if "reader" in data:
        req.reader = {"name": _str(data, "reader")}
    if "enhancer" in data:
        req.enhancer = {"name": _str(data, "enhancer")}
        if data.get("enhancedPrompt") is not None:
            req.enhanced_prompt = str(data["enhancedPrompt"])
    if isinstance(data.get("inserts"), list):
        req.inserts = [d for d in data["inserts"] if isinstance(d, dict)]
    if data.get("seconds") is not None:
        try:
            req.seconds = float(data["seconds"])
        except (TypeError, ValueError):
            pass
    return GenRecord(_str(data, "output"), _str(data, "source"), _str(data, "time"), req)


# ----------------------------------------------------------------------
# ワークフロー (ComfyUI の「Export (API)」で保存した JSON)
# ----------------------------------------------------------------------
def load_workflow(path: str) -> dict:
    if not path or not Path(path).is_file():
        raise ComfyError(f"ワークフローのファイルが見つかりません。\n{path}")
    try:
        wf = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ComfyError(f"ワークフローの JSON を読めません。\n{exc}") from exc
    if (
        not isinstance(wf, dict)
        or "nodes" in wf
        or not any(isinstance(v, dict) and "class_type" in v for v in wf.values())
    ):
        raise ComfyError(
            "このファイルは API 形式のワークフローではありません。\n"
            "ComfyUI のメニュー「Export (API)」(Save (API Format)) で保存した JSON を指定してください。\n"
            f"{path}"
        )
    return wf


def _nodes(wf: dict):
    for node in wf.values():
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            yield node


def _class_type(node: dict) -> str:
    value = node.get("class_type")
    return value if isinstance(value, str) else ""


def _title(node: dict) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title if isinstance(title, str) else ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def count_nodes(wf: dict, class_type: str) -> int:
    return sum(1 for node in _nodes(wf) if _class_type(node) == class_type)


def is_video_workflow(wf: dict) -> bool:
    """名前に video を含むノードか、動く画像を保存するノードがあれば動画用。"""
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ct = _class_type(node)
        if "video" in ct.lower() or ct in ("SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG"):
            return True
    return False


PROMPT_TITLES = ("positive", "user prompt")  # 文字列ノードのタイトルにこれがあれば本文を入れる


def _is_negative(node: dict) -> bool:
    return "negative" in _title(node).lower()


def _clip_text_nodes(wf: dict) -> list[dict]:
    """文字列が直接入っている CLIPTextEncode (ネガティブ用は除く)。"""
    return [n for n in _nodes(wf)
            if _class_type(n) == "CLIPTextEncode" and isinstance(n["inputs"].get("text"), str)
            and not _is_negative(n)]


PROMPT_MARKER = "{prompt}"  # ワークフローの文字列にこれがあれば、そこを本文に置き換える


def _is_link(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def _switch_unused_key(inputs: dict) -> Optional[str]:
    """If/Else Switch (on_true / on_false) で、今の switch の値では使われない側の入力名。
    switch が線でつながっているなど決まらないときは None (どちらも使われるとみなす)。"""
    if "on_true" in inputs and "on_false" in inputs and isinstance(inputs.get("switch"), bool):
        return "on_false" if inputs["switch"] else "on_true"
    return None


def _used_node_ids(wf: dict) -> set:
    """実際に実行されるノード。どこからも使われていないノード (保存など) から線を逆にたどる。
    Switch は使われない側の線をたどらない。"""
    referenced = {v[0] for node in _nodes(wf) for v in node["inputs"].values() if _is_link(v)}
    stack = [nid for nid, node in wf.items()
             if isinstance(node, dict) and isinstance(node.get("inputs"), dict) and nid not in referenced]
    used = set()
    while stack:
        nid = stack.pop()
        if nid in used or not isinstance(wf.get(nid), dict):
            continue
        used.add(nid)
        inputs = wf[nid].get("inputs")
        if not isinstance(inputs, dict):
            continue
        unused = _switch_unused_key(inputs)
        stack.extend(v[0] for k, v in inputs.items() if k != unused and _is_link(v))
    return used


def _is_used_slot(wf: dict, used: set, inputs: dict, key: str) -> bool:
    for nid in used:
        if wf[nid].get("inputs") is inputs:
            return key != _switch_unused_key(inputs)
    return False


def _marker_slots(wf: dict) -> list[tuple[dict, str]]:
    """文字列に {prompt} の目印が入っている入力 (ノードの種類・タイトルは問わない)。"""
    return [(node["inputs"], key) for node in _nodes(wf)
            for key, value in node["inputs"].items()
            if isinstance(value, str) and PROMPT_MARKER in value]


def _prompt_slots(wf: dict) -> list[tuple[dict, str]]:
    """本文を入れる場所のうち、実際に使われるもの (Switch で使われない側などは除く)。"""
    used = _used_node_ids(wf)
    return [(inputs, key) for inputs, key in _prompt_slot_candidates(wf)
            if _is_used_slot(wf, used, inputs, key)]


def _prompt_slot_candidates(wf: dict) -> list[tuple[dict, str]]:
    """本文を入れる場所の候補。{prompt} の目印があればそこ (すべて。目印の部分だけを置き換える)。
    目印がなければ、次のどちらかがあればそこ (すべて):
      - 文字列が直接入っている prompt 入力
      - 文字列ノード (PrimitiveStringMultiline など) で、タイトルに Positive / User Prompt とあるもの
        (ComfyUI のテンプレートは、ここから TextGenerate や CLIP Text Encode へ線でつないでいる)
    なければ CLIPTextEncode の text: 1 つならそれ、複数ならタイトルに Positive とあるもの。
    タイトルに Negative とあるノードには入れない。"""
    marked = _marker_slots(wf)
    if marked:
        return marked
    slots = []
    for node in _nodes(wf):
        inputs = node["inputs"]
        if _is_negative(node):
            continue
        if isinstance(inputs.get("prompt"), str):
            slots.append((inputs, "prompt"))
        elif (_class_type(node).startswith("PrimitiveString") and isinstance(inputs.get("value"), str)
              and any(t in _title(node).lower() for t in PROMPT_TITLES)):
            slots.append((inputs, "value"))
    if slots:
        return slots
    clip = _clip_text_nodes(wf)
    pick = clip if len(clip) == 1 else [n for n in clip if "positive" in _title(n).lower()]
    return [(n["inputs"], "text") for n in pick]


def has_prompt(wf: dict) -> bool:
    return bool(_prompt_slots(wf))


def prompt_slot_labels(wf: dict) -> list[str]:
    """本文を入れる場所の説明 (図の画面の確認用)。例: ノード 6「Positive」の text"""
    labels = []
    for inputs, key in _prompt_slots(wf):
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("inputs") is inputs:
                name = _title(node) or _class_type(node)
                part = f" (「{PROMPT_MARKER}」の部分)" if PROMPT_MARKER in str(inputs.get(key, "")) else ""
                labels.append(f"ノード {nid}「{name}」の {key}{part}")
                break
    return labels


def prompt_slot_problem(wf: dict) -> Optional[str]:
    """プロンプトを入れる場所がなければ、その理由 (あれば None)。"""
    if _prompt_slots(wf):
        return None
    if _marker_slots(wf):
        return (
            f"このワークフローの「{PROMPT_MARKER}」の目印は、どれも実際には使われない場所にあります "
            "(If/Else Switch の使われない側など)。\n"
            f"ComfyUI で、画像に使われる文字欄に「{PROMPT_MARKER}」を書いてから、もう一度「Export (API)」で保存してください。"
        )
    if _prompt_slot_candidates(wf):
        return (
            "このワークフローでプロンプトを入れる場所は、実際には使われていません "
            "(If/Else Switch の使われない側につながっているなど)。\n"
            f"ComfyUI で、画像に使われる文字欄に「{PROMPT_MARKER}」と書いてから、もう一度「Export (API)」で保存してください。"
            f"\n「{PROMPT_MARKER}」があれば、そこをプロンプト (エンハンサーを使うときは盛ったプロンプト) に置き換えます"
            "(前後に書いた語はそのまま残ります)。"
        )
    if len(_clip_text_nodes(wf)) > 1:
        return (
            "このワークフローには「CLIP Text Encode」が複数あり、どれにプロンプトを入れるか決められません。\n"
            "ComfyUI で、プロンプトを入れるノードのタイトルに「Positive」を含めてから、もう一度「Export (API)」で保存してください。"
        )
    return (
        "このワークフローには、プロンプトを入れる場所がありません。\n"
        "文字列が直接入っている prompt 入力か、「CLIP Text Encode」のあるワークフローを指定してください。\n"
        "プロンプトを文字列ノード (Text など) から線でつないでいるときは、そのノードのタイトルに"
        "「Positive」か「User Prompt」を含めてください (「Negative」とあるノードには入れません)。\n"
        f"どのノードでも、文字欄に「{PROMPT_MARKER}」と書けば、そこをプロンプト (エンハンサーを使うときは盛ったプロンプト) に置き換えます。"
    )


def _duration_slot(wf: dict) -> tuple[Optional[dict], str, Optional[dict]]:
    """長さを入れる場所 (inputs, キー, ノード)。
    タイトルに duration とある Float / Int ノードの value。なければ、数値が直接入っている duration 入力。"""
    direct = None
    for node in _nodes(wf):
        inputs = node["inputs"]
        if (
            _class_type(node) in ("PrimitiveFloat", "PrimitiveInt")
            and "duration" in _title(node).lower()
            and _is_number(inputs.get("value"))
        ):
            return inputs, "value", node
        if direct is None and _is_number(inputs.get("duration")):
            direct = (inputs, "duration", node)
    return direct if direct else (None, "duration", None)


def get_duration(wf: dict) -> Optional[float]:
    inputs, key, _node = _duration_slot(wf)
    return None if inputs is None else float(inputs[key])


def duration_is_integer(wf: dict) -> bool:
    inputs, key, node = _duration_slot(wf)
    return inputs is not None and key == "value" and _class_type(node) == "PrimitiveInt"


def _set_duration(wf: dict, seconds: float) -> None:
    inputs, key, _node = _duration_slot(wf)
    if inputs is not None:
        # 端数がなければ整数で入れる (整数の入力でも小数の入力でも通る)
        inputs[key] = int(seconds) if seconds == int(seconds) else seconds


def _randomize_seeds(wf: dict) -> None:
    for node in _nodes(wf):
        inputs = node["inputs"]
        for key in ("seed", "noise_seed"):
            if _is_number(inputs.get(key)):
                inputs[key] = _rng.randrange(0, 10**15)


def describe_workflow(wf: dict) -> str:
    """設定画面に出す確認結果。"""
    kind = "動画用" if is_video_workflow(wf) else "画像用"
    slots = len(_prompt_slots(wf))
    where = f"「{PROMPT_MARKER}」の目印" if _marker_slots(wf) else "プロンプトを入れる場所"
    prompt = f"{where} {slots} か所" if slots else "プロンプトを入れる場所がありません"
    duration = "長さを変えられます" if get_duration(wf) is not None else "長さは変えられません"
    text = f"ワークフローを確認しました ({kind}、ノード {len(wf)} 個、{prompt}、{duration})"
    if not app.profile().reads:
        if count_nodes(wf, "LoadImage"):
            text += "\n※「Load Image」ノードがあります。画像はワークフローに書かれているものがそのまま使われます。"
    elif uses_source_image(wf):
        text += "\n「Load Image」ノードがあるので、元画像も送って入れます。"
    else:
        text += "\n元画像は送らず、プロンプトだけで生成します。"
    return text


def _load_image_nodes(wf: dict) -> list[dict]:
    """元画像を入れる場所: 実際に使われる LoadImage ノード。"""
    used = _used_node_ids(wf)
    return [wf[nid] for nid in used if _class_type(wf[nid]) == "LoadImage" and "image" in wf[nid]["inputs"]]


def uses_source_image(wf: dict) -> bool:
    """ワークフローに LoadImage があり、元画像も送るか (元画像のあるアプリだけ)。"""
    return app.profile().reads and bool(_load_image_nodes(wf))


# ----------------------------------------------------------------------
# ComfyUI の HTTP API
#   1. POST /upload/image で元画像を送る (元画像のあるアプリで、ワークフローに LoadImage があるときだけ)
#   2. ワークフローにプロンプト・長さ・元画像を入れ、シードをランダムにする
#   3. POST /prompt       で実行を依頼
#   4. GET  /history/{id} で完了を待つ (待ち中は GET /queue で順番を表示)
#   5. GET  /view         で出来たファイルを受け取り、アプリが決めた場所に保存
# ----------------------------------------------------------------------
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # ローカル / LAN にプロキシを通さない


def _request(url: str, method: str = "GET", body: Optional[bytes] = None, timeout: float = 30,
             content_type: str = "application/json") -> bytes:
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with _opener.open(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        # 400 などは本文に理由 (node_errors など) が入っているので、それを見せる
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except OSError:
            text = ""
        raise ComfyError(f"ComfyUI がエラーを返しました ({exc.code})。\n{_shorten(text)}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ComfyConnectionError(str(reason)) from exc


def _json(url: str, method: str = "GET", body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw = _request(url, method, data)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _upload_image(base_url: str, source: str) -> str:
    """元画像を ComfyUI の input に送り、LoadImage に入れる名前を返す。
    送る名前は英数字だけの一時的なものにする (日本語のファイル名で失敗しないように)。"""
    try:
        data = Path(source).read_bytes()
    except OSError as exc:
        raise ComfyError(f"元画像を読めません。\n{source}\n{exc}") from exc
    ext = Path(source).suffix.lower()
    if not ext[1:].isalnum():
        ext = ".png"
    prefix = "".join(c for c in app.profile().name.lower() if c.isalnum()) or "gencommon"
    name = prefix + "_" + uuid.uuid4().hex[:12] + ext
    boundary = "----" + prefix + uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n").encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    raw = _request(base_url + "/upload/image", "POST", b"".join(parts), timeout=120,
                   content_type=f"multipart/form-data; boundary={boundary}")
    try:
        res = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        res = None
    if not isinstance(res, dict) or not res.get("name"):
        text = raw.decode("utf-8", errors="replace")
        raise ComfyError(f"ComfyUI が元画像を受け付けませんでした。\n{_shorten(text)}")
    subfolder = str(res.get("subfolder") or "")
    return f"{subfolder}/{res['name']}" if subfolder else str(res["name"])


def test_connection(base_url: str) -> Optional[str]:
    """ComfyUI が動いているか確かめる。戻り値はエラーメッセージ (正常なら None)。"""
    try:
        _request((base_url or "").rstrip("/") + "/system_stats")
        return None
    except ComfyError as exc:
        return str(exc)


def generate(
    source: str,
    req: GenRequest,
    progress: Callable[[str], None],
    cancel: threading.Event,
    prompt_id: str = "",
    on_submitted: Optional[Callable[[str], None]] = None,
    on_run_time: Optional[Callable[[float], None]] = None,
    held: Optional[Callable[[], bool]] = None,
) -> list[str]:
    """生成して、保存したファイルのパスを返す (動画なら 1 つ、画像なら出力された枚数ぶん)。

    source は元のファイル (元画像のあるアプリで、ワークフローに LoadImage があれば、それも送って入れる)。

    prompt_id を渡すと、前に依頼したその実行の続きを待つ (アプリを閉じる・PC が落ちるなどで
    受け取れなかったもの)。ComfyUI 側にもう残っていなければ、最初から依頼し直す。
    on_submitted(prompt_id) は、ComfyUI が実行を受け付けたときに呼ぶ。
    on_run_time(秒) は、ComfyUI の記録から実行にかかった時間が分かったときに呼ぶ (順番待ちは含まない)。
    held() が True を返すあいだ (一時停止中) は、依頼し直さず・接続が切れても失敗にせず GenerationHeld。

    失敗したときは ComfyError、cancel で止められたときは ComfyUI 側も取り消して GenerationCancelled。
    """
    base_url = (req.url or DEFAULT_URL).rstrip("/")

    def submit() -> str:
        wf = load_workflow(req.workflow_path)
        _randomize_seeds(wf)
        load_nodes = _load_image_nodes(wf) if app.profile().reads else []
        if load_nodes:
            if cancel.is_set():
                raise GenerationCancelled()
            progress("元画像を ComfyUI に送っています...")
            uploaded = _upload_image(base_url, source)
            for node in load_nodes:
                node["inputs"]["image"] = uploaded
        prompt = req.full_prompt
        if prompt is not None:
            marked = bool(_marker_slots(wf))
            for inputs, key in _prompt_slots(wf):
                inputs[key] = inputs[key].replace(PROMPT_MARKER, prompt) if marked else prompt
        if req.seconds is not None:
            _set_duration(wf, req.seconds)

        if cancel.is_set():
            raise GenerationCancelled()
        progress("ComfyUI に実行を依頼中...")
        res = _json(
            base_url + "/prompt",
            "POST",
            {"prompt": wf, "client_id": app.profile().name.lower() + "-" + uuid.uuid4().hex},
        )
        new_id = res.get("prompt_id") if isinstance(res, dict) else None
        if not new_id:
            raise ComfyError(f"ComfyUI が実行を受け付けませんでした。\n{_shorten(str(res))}")
        if on_submitted is not None:
            on_submitted(str(new_id))
        return str(new_id)

    if prompt_id:
        progress("前回の続きを確かめています...")
        delay = 0  # 続きのときは、すぐに様子を見る
    else:
        prompt_id = submit()
        delay = 2

    started = time.monotonic()  # 依頼した時刻 (順番待ちの時間はここから)
    run_started = None  # 「生成中」になった時刻 (生成中の時間はここから数え直す)
    failures = 0
    while True:
        if cancel.wait(delay):
            _cancel_prompt(base_url, prompt_id)
            raise GenerationCancelled()
        delay = 2
        try:
            # 待ち行列を先に見てから記録を見る (その間に終わっても、記録のほうで拾える)
            state = _queue_state(base_url, prompt_id)
            history = _json(base_url + "/history/" + urllib.parse.quote(prompt_id, safe=""))
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                break
            failures = 0
            if state is None:
                # 待ち行列にも記録にもない (ComfyUI を再起動したなど): 最初から依頼し直す
                if held is not None and held():
                    raise GenerationHeld(sent=False)
                prompt_id = submit()
                started = time.monotonic()
                run_started = None
                continue
            if state == "生成中":
                # 順番待ちの時間は含めない (あと何分で終わるかの目安にするため)
                if run_started is None:
                    run_started = time.monotonic()
                progress(f"{state} {_elapsed(run_started)}")
            else:
                run_started = None
                progress(f"{state} {_elapsed(started)}")
        except ComfyConnectionError:
            # ComfyUI が落ちた・再起動中など。1 分ほど続いたらあきらめる
            failures += 1
            if failures >= 30:
                if held is not None and held():
                    raise GenerationHeld(sent=True)  # 再開したときに、まだ残っていれば受け取る
                raise ComfyError(f"ComfyUI との接続が切れました。({base_url})")
            progress(f"ComfyUI の応答待ち... {_elapsed(started if run_started is None else run_started)}")

    _check_status(entry)
    run_time = _run_seconds(entry)
    if run_time is not None and on_run_time is not None:
        on_run_time(run_time)
    outputs = _find_outputs(entry)
    if not outputs:
        raise ComfyError(
            "ワークフローの出力に動画や画像のファイルが見つかりません。\n"
            "Save Video / Save Image などの保存ノードがあるか確認してください。"
        )

    # 置き場所と名前はアプリが決める (PromptWordCrafter は元と同じフォルダ、ImageMimicArt は出力フォルダの下)
    src = Path(source)
    output_path = app.profile().output_path
    for output in outputs:  # 受け取る前に、置き場所を作れるか確かめておく
        folder = output_path(src, req, output["video"], Path(output["filename"]).suffix).parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ComfyError(f"保存先のフォルダを作れません。\n{folder}\n{exc}") from exc
    saved = []
    for output in outputs:
        if cancel.is_set():
            raise GenerationCancelled()
        if len(outputs) > 1:
            progress(f"結果を受け取り中... ({len(saved) + 1} / {len(outputs)})")
        else:
            progress("結果を受け取り中...")
        query = urllib.parse.urlencode(
            {"filename": output["filename"], "subfolder": output["subfolder"], "type": output["type"]}
        )
        data = _request(base_url + "/view?" + query, timeout=120)
        # 同時に終わったジョブが同じ名前を選ばないよう、名前を決めてから置くまでを 1 件ずつ行う
        with _save_lock:
            dest = _unique_path(output_path(src, req, output["video"], Path(output["filename"]).suffix))
            temp = dest.with_name(dest.name + ".part")
            try:
                temp.write_bytes(data)
                temp.replace(dest)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass
        saved.append(str(dest))
    return saved


def cancel_prompt(url: str, prompt_id: str) -> None:
    """ComfyUI に依頼済みの実行を取り消す (つながらなければ何もしない)。"""
    try:
        _cancel_prompt((url or DEFAULT_URL).rstrip("/"), prompt_id)
    except ComfyError:
        pass


def _cancel_prompt(base_url: str, prompt_id: str) -> None:
    """待ち行列から外し、ちょうど自分の番で動いているときだけ中断を送る。"""
    try:
        _json(base_url + "/queue", "POST", {"delete": [prompt_id]})
    except ComfyError:
        pass
    try:
        queue = _json(base_url + "/queue")
        if isinstance(queue, dict) and _contains_prompt(queue.get("queue_running"), prompt_id):
            _json(base_url + "/interrupt", "POST", {"prompt_id": prompt_id})
    except ComfyError:
        pass


def _contains_prompt(items, prompt_id: str) -> bool:
    return isinstance(items, list) and any(
        isinstance(x, list) and len(x) >= 2 and str(x[1]) == prompt_id for x in items
    )


def _queue_state(base_url: str, prompt_id: str) -> Optional[str]:
    """待ち行列での様子。待ち行列にいないと分かったときは None。

    接続できないときは ComfyConnectionError をそのまま投げる (呼び出し元で待ち続ける)。
    """
    try:
        queue = _json(base_url + "/queue")
    except ComfyConnectionError:
        raise
    except ComfyError:
        return "生成中"
    if not isinstance(queue, dict):
        return "生成中"
    if _contains_prompt(queue.get("queue_running"), prompt_id):
        return "生成中"
    pending = [x for x in queue.get("queue_pending") or [] if isinstance(x, list) and len(x) >= 2]
    pending.sort(key=lambda x: x[0] if _is_number(x[0]) else 0)
    for i, item in enumerate(pending):
        if str(item[1]) == prompt_id:
            return f"順番待ち ({i + 1} 番目)"
    return None


def _check_status(entry: dict) -> None:
    status = entry.get("status")
    if not isinstance(status, dict) or status.get("status_str") != "error":
        return
    detail = ""
    for message in status.get("messages") or []:
        if not isinstance(message, list) or len(message) < 2 or message[0] != "execution_error":
            continue
        data = message[1] if isinstance(message[1], dict) else {}
        node_type = data.get("node_type")
        text = str(data.get("exception_message") or "").strip()
        detail = (f"[{node_type}] " if node_type else "") + text
    raise ComfyError(f"ComfyUI での生成中にエラーが起きました。\n{_shorten(detail)}")


def _run_seconds(entry: dict) -> Optional[float]:
    """記録の execution_start から execution_success までの秒数 (分からなければ None)。"""
    status = entry.get("status")
    stamps = {}
    for message in (status.get("messages") or []) if isinstance(status, dict) else []:
        if (isinstance(message, list) and len(message) >= 2 and isinstance(message[1], dict)
                and _is_number(message[1].get("timestamp"))):
            stamps[message[0]] = message[1]["timestamp"]  # ミリ秒
    start, end = stamps.get("execution_start"), stamps.get("execution_success")
    if start is None or end is None or end < start:
        return None
    return (end - start) / 1000


def _find_outputs(entry: dict) -> list[dict]:
    """動画があれば 1 つだけ (mp4 を優先)。なければ画像をすべて (保存用があればそれだけ)。"""
    found = []
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return found
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                name = str(item["filename"])
                ext = Path(name).suffix.lower()
                video = ext in VIDEO_EXTS
                if not video and ext not in IMAGE_EXTS:
                    continue
                found.append(
                    {
                        "filename": name,
                        "subfolder": str(item.get("subfolder") or ""),
                        "type": str(item.get("type") or "output"),
                        "video": video,
                    }
                )
    videos = [f for f in found if f["video"]]
    if videos:
        videos.sort(
            key=lambda f: (f["type"] != "output", VIDEO_EXTS.index(Path(f["filename"]).suffix.lower()))
        )
        return [videos[0]]
    kept = [f for f in found if f["type"] == "output"]
    return kept or found


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def format_elapsed(seconds: float) -> str:
    """「1:05」の形 (1 時間を超えたら「1:02:05」)。"""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _elapsed(started: float) -> str:
    return format_elapsed(time.monotonic() - started)


def _shorten(text: str) -> str:
    text = (text or "").strip()
    return text[:800] + "..." if len(text) > 800 else text
