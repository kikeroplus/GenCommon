"""差し込み: フォルダのテキストを、ジョブ 1 件ごとに 1 つずつ順にプロンプトへ足す (generation-standard 6 章)。

- 枠は 2 つ (初めはセリフ・アクション)。枠ごとに 名前・フォルダ・使うか・盛る前に入れるか を持つ。
- フォルダの .txt を名前順 (自然順) に使い、最後まで使ったら最初に戻る。空のファイルは飛ばす。
- どこまで使ったかは、フォルダごとに最後に使ったファイル名で覚える (この PC の %APPDATA%\\GenCommon。
  3 つのアプリで共通)。ファイルを足したり消したりしても、名前順で続きから使う。
- ファイルを割り当てるのは「生成する」でジョブを登録したとき (続きから始めても変わらない)。

ジョブ・記録に持たせる形 (GenRequest.inserts): 使った枠ごとに
  {"slot": 番号, "name": 名前, "folder": フォルダ, "before": 盛る前か, "file": ファイル名, "text": 中身}
生成の画面の欄の形 (前回の値・セット・入力中の内容): 枠ごとに
  {"slot": 番号, "name": 名前, "folder": フォルダ, "before": 盛る前か, "use": 使うか}
"""

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import store

SLOT_COUNT = 2
DEFAULT_NAMES = ("セリフ", "アクション")
SLOTS_FILE = "insert_slots.json"  # 前回の枠の設定 (アプリごと)
POSITIONS_FILE = "insert_positions.txt"  # フォルダごとに最後に使ったファイル名 (1 行に「フォルダ Tab ファイル名」)
EXTENSION = ".txt"
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "utf-16", "latin-1")  # PromptWordCrafter と同じ順

_lock = threading.Lock()


class InsertError(Exception):
    """差し込みのフォルダが使えない。"""


@dataclass
class Slot:
    name: str = ""
    folder: str = ""
    use: bool = False
    before: bool = False  # True = 盛る前に入れる (エンハンサーに渡す)。False = 盛ったあとに付ける

    def label(self, index: int) -> str:
        return self.name.strip() or default_name(index)

    def to_dict(self, index: int) -> dict:
        return {"slot": index, "name": self.name, "folder": self.folder, "before": self.before, "use": self.use}


def default_name(index: int) -> str:
    return DEFAULT_NAMES[index] if 0 <= index < len(DEFAULT_NAMES) else f"差し込み {index + 1}"


def default_slots() -> list[Slot]:
    return [Slot(name=default_name(i)) for i in range(SLOT_COUNT)]


# ----------------------------------------------------------------------
# 欄の形 ⇔ Slot
# ----------------------------------------------------------------------
def apply(slots: list[Slot], data) -> list[Slot]:
    """欄の形・ジョブの形の並びを slots に当てた写しを返す。

    欄の形 ("use" あり) は、その枠の中身をすべて入れ替える。
    ジョブ・記録の形 ("use" なし) は、出てくる枠を使う・出てこない枠を使わないにする (名前・フォルダは残す)。
    data が並びでなければ slots のまま。
    """
    result = [Slot(s.name, s.folder, s.use, s.before) for s in slots]
    if not isinstance(data, list):
        return result
    items = [d for d in data if isinstance(d, dict)]
    from_job = not any("use" in d for d in items)
    if from_job:
        for s in result:
            s.use = False
    for d in items:
        try:
            i = int(d.get("slot", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(result):
            continue
        s = result[i]
        s.name = str(d.get("name") or s.name)
        s.folder = str(d.get("folder") or "")
        s.before = bool(d.get("before"))
        s.use = True if from_job else bool(d.get("use"))
    return result


def to_data(slots: list[Slot]) -> list[dict]:
    return [s.to_dict(i) for i, s in enumerate(slots)]


def load_slots() -> list[Slot]:
    """前回の枠の設定 (なければ初めの名前で、使わない)。"""
    try:
        data = json.loads(store.load(SLOTS_FILE) or "[]")
    except ValueError:
        data = []
    return apply(default_slots(), data)


def save_slots(slots: list[Slot]) -> None:
    store.save(SLOTS_FILE, json.dumps(to_data(slots), ensure_ascii=False))


# ----------------------------------------------------------------------
# フォルダの中身
# ----------------------------------------------------------------------
def natural_key(text: str):
    """名前順の比べ方 (大文字小文字を区別せず、半角の数字は数として比べる: 2.txt < 10.txt)。

    J-Rate (ComfyInserts.cs の NaturalCompare) と同じ並びにすること (どこまで使ったかを 2 つで共有するので)。
    """
    return [(0, int(part), "") if part[0] in "0123456789" else (1, 0, part)
            for part in re.split(r"([0-9]+)", text.lower()) if part]


def list_files(folder: str) -> list[Path]:
    """フォルダの .txt を名前順に (サブフォルダは見ない)。読めなければ InsertError。"""
    path = Path(folder.strip()) if folder and folder.strip() else None
    if path is None:
        raise InsertError("フォルダが決まっていません")
    if not path.is_dir():
        raise InsertError(f"フォルダが見つかりません ({path})")
    try:
        files = [p for p in path.iterdir() if p.suffix.lower() == EXTENSION and p.is_file()]
    except OSError as exc:
        raise InsertError(f"フォルダを読めません ({exc})") from exc
    if not files:
        raise InsertError(f"フォルダにテキスト ({EXTENSION}) がありません ({path})")
    return sorted(files, key=lambda p: (natural_key(p.name), p.name))


def read_file(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding).replace("\r\n", "\n").strip()
        except UnicodeDecodeError:
            continue
    return ""


def _folder_key(folder: str) -> str:
    return os.path.normcase(os.path.abspath(folder.strip()))


# ----------------------------------------------------------------------
# どこまで使ったか (この PC の %APPDATA%\GenCommon。3 つのアプリで共通)
# ----------------------------------------------------------------------
def _positions_path() -> Path:
    return store.local_dir() / POSITIONS_FILE


def load_positions() -> dict[str, str]:
    text = store.read_text(_positions_path()) or ""
    result = {}
    for line in text.splitlines():
        if "\t" in line:
            key, name = line.split("\t", 1)
            if key.strip() and name.strip():
                result[key.strip()] = name.strip()
    return result


def save_positions(positions: dict[str, str]) -> None:
    try:
        store.write_text(_positions_path(), "".join(f"{k}\t{v}\r\n" for k, v in positions.items()))
    except OSError:
        pass


def last_used(folder: str) -> str:
    return load_positions().get(_folder_key(folder), "")


def reset(folder: str) -> None:
    """そのフォルダを最初から使い直す。"""
    with _lock:
        positions = load_positions()
        if positions.pop(_folder_key(folder), None) is not None:
            save_positions(positions)


def _next_index(files: list[Path], last: str) -> int:
    """last の次 (名前順)。last がなければ・最後なら最初。"""
    if not last:
        return 0
    key = (natural_key(last), last)
    for i, p in enumerate(files):
        if (natural_key(p.name), p.name) > key:
            return i
    return 0


@dataclass
class Peek:
    files: list[Path]
    index: int  # 次に使うファイルの番号 (0 始まり)

    @property
    def next_file(self) -> Path:
        return self.files[self.index]

    def describe(self) -> str:
        return f"次: {self.next_file.name} ({self.index + 1} / {len(self.files)})"


def peek(folder: str) -> Peek:
    """次に使うファイル (割り当てはしない)。使えなければ InsertError。"""
    files = list_files(folder)
    return Peek(files, _next_index(files, last_used(folder)))


def problem(slot: Slot) -> Optional[str]:
    """使う枠が使えない理由 (使えれば None)。"""
    if not slot.use:
        return None
    try:
        peek(slot.folder)
    except InsertError as exc:
        return str(exc)
    return None


# ----------------------------------------------------------------------
# ジョブへの割り当て
# ----------------------------------------------------------------------
class Picker:
    """ジョブ 1 件ごとに、使う枠の次のファイルを 1 つずつ渡す。全部割り当ててから commit() で位置を書く。

    同じフォルダを 2 つの枠で使ったときは、同じ並びを続けて使う (位置はフォルダごと)。
    J-Rate (ComfyInserts.cs の Inserts.Picker) と同じ動き。
    """

    def __init__(self, slots: list[Slot]):
        self.slots = [(i, s) for i, s in enumerate(slots) if s.use]
        self._files: dict[str, list[Path]] = {}
        self._texts: dict[str, str] = {}
        with _lock:
            self._positions = load_positions()
        self._touched: set[str] = set()
        for i, s in self.slots:
            key = _folder_key(s.folder)
            if key not in self._files:
                try:
                    self._files[key] = list_files(s.folder)
                except InsertError as exc:
                    raise InsertError(f"差し込み「{s.label(i)}」: {exc}") from exc

    @property
    def active(self) -> bool:
        return bool(self.slots)

    def _text(self, path: Path) -> str:
        key = str(path)
        if key not in self._texts:
            self._texts[key] = read_file(path)
        return self._texts[key]

    def next(self) -> Optional[list[dict]]:
        """次のジョブの差し込み (使う枠がなければ None)。中身のあるファイルがなければ InsertError。"""
        if not self.slots:
            return None
        result = []
        for i, s in self.slots:
            key = _folder_key(s.folder)
            files = self._files[key]
            index = _next_index(files, self._positions.get(key, ""))
            for _ in range(len(files)):
                path = files[index]
                self._positions[key] = path.name
                self._touched.add(key)
                text = self._text(path)
                if text:
                    break
                index = (index + 1) % len(files)
            else:
                raise InsertError(f"差し込み「{s.label(i)}」: フォルダのテキストがすべて空です ({s.folder})")
            result.append({"slot": i, "name": s.label(i), "folder": s.folder, "before": s.before,
                           "file": path.name, "text": text})
        return result

    def commit(self) -> None:
        """使ったフォルダの位置を書く (ほかのアプリが書いたものは、読み直して残す)。"""
        if not self._touched:
            return
        with _lock:
            now = load_positions()
            for key in self._touched:
                now[key] = self._positions[key]
            save_positions(now)

    def discard(self) -> None:
        """割り当てをやめる (位置は書かない)。"""
        self._touched.clear()


# ----------------------------------------------------------------------
# 組み立て (GenRequest から使う)
# ----------------------------------------------------------------------
def texts(items: Optional[list], before: bool) -> list[str]:
    return [str(d.get("text") or "") for d in (items or [])
            if isinstance(d, dict) and bool(d.get("before")) == before]


def join(*parts: Optional[str]) -> str:
    """空でないものを改行でつなぐ。"""
    return "\n".join(p.strip() for p in parts if p and p.strip())


def summary(items: Optional[list]) -> str:
    """「セリフ: 003.txt、アクション: 012.txt」 (ジョブ一覧・記録の見出し用)。"""
    return "、".join(f"{d.get('name', '')}: {d.get('file', '')}" for d in (items or []) if isinstance(d, dict))
