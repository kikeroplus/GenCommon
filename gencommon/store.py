"""設定ファイルの置き場所。共有するものは %APPDATA%\\GenCommon、それ以外はアプリごとのフォルダ。

generation-standard.md の 8・11 章:
- 接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセット・LLM の接続は
  J-Rate・PromptWordCrafter・ImageMimicArt で共有する (どのアプリで変えても、ほかの 2 つに効く)。
- 使用中の番号・前回の値・セット・記録・ジョブはアプリごと。

ファイルの名前は共有でもアプリごとでも同じなので、呼び出し側は名前だけを渡せばよい
(file_path() が名前で置き場所を振り分ける)。

テストで差し替えられるよう、APP_DIR / SHARED_DIR はモジュール変数にしておく。
"""

import json
import os
import threading
from pathlib import Path
from typing import Optional

_ROOT = Path(os.environ.get("APPDATA") or Path.home())
SHARED_DIR = _ROOT / "GenCommon"
APP_NAME = ""
APP_DIR = _ROOT / "GenCommon-unconfigured"

# 共有するファイル (generation-standard 8 章「共有」)
CONNECTIONS_FILE = "comfy_presets.txt"
WORKFLOWS_FILE = "comfy_workflows.txt"
BASE_VIDEO_FILE = "comfy_base_prompt_video.txt"
BASE_IMAGE_FILE = "comfy_base_prompt_image.txt"
ENHANCER_FILE = "enhancer_presets.json"
READER_FILE = "reader_presets.json"
LLM_FILES = ("llm_url.txt", "reader_model.txt", "enhance_model.txt", "reader_max_side.txt")
APPS_FILE = "apps.txt"
SHARED_FILES = frozenset(
    (CONNECTIONS_FILE, WORKFLOWS_FILE, BASE_VIDEO_FILE, BASE_IMAGE_FILE, ENHANCER_FILE, READER_FILE, APPS_FILE)
    + LLM_FILES
)

# 共有の一覧を指す、アプリごとの番号のファイル (上へ / 下へで入れ替えたときに直す)
INDEX_FILES = {
    CONNECTIONS_FILE: ("comfy_preset.txt",),
    WORKFLOWS_FILE: ("comfy_workflow_preset.txt",),
    ENHANCER_FILE: ("enhancer_generate.txt", "enhance_preset.txt"),
    READER_FILE: ("reader_generate.txt", "reader_preset.txt"),
}

MERGED_FILE = "gencommon_merged.txt"  # このアプリの設定を共有に移した印

_write_lock = threading.Lock()


def configure(app_name: str) -> None:
    """アプリの名前を決める (起動したら最初に呼ぶ)。アプリごとのファイルは %APPDATA%\\(アプリ名) に置く。"""
    global APP_NAME, APP_DIR
    APP_NAME = app_name
    APP_DIR = _ROOT / app_name


def is_shared(name: str) -> bool:
    return name in SHARED_FILES


def file_path(name: str) -> Path:
    return (SHARED_DIR if is_shared(name) else APP_DIR) / name


def app_file(name: str) -> Path:
    """共有するファイルの名前でも、アプリのフォルダのほう (引き継ぎ前の古いファイル)。"""
    return APP_DIR / name


def read_text(path: Path) -> Optional[str]:
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None


def write_text(path: Path, value: str) -> None:
    """別名に書いてから置き換える (書きかけで落ちても、ほかのアプリが読みかけでも壊れないように)。失敗したら OSError。"""
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(value, encoding="utf-8", newline="")
        temp.replace(path)


def load(name: str) -> Optional[str]:
    return read_text(file_path(name))


def save(name: str, value: str) -> None:
    try:
        write_text(file_path(name), value)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 共有を使っているアプリ (番号の入れ替えを伝える先)
# ----------------------------------------------------------------------
def register_app() -> None:
    """このアプリの設定フォルダを apps.txt に書く (なければ)。"""
    dirs = app_dirs()
    if any(_same(d, APP_DIR) for d in dirs):
        return
    save(APPS_FILE, "".join(f"{d}\r\n" for d in dirs + [APP_DIR]))


def app_dirs() -> list[Path]:
    text = load(APPS_FILE) or ""
    return [Path(line.strip()) for line in text.splitlines() if line.strip()]


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def swap_indexes(shared_file: str, a: int, b: int) -> None:
    """共有の一覧の a 番と b 番 (0 始まり) を入れ替えたとき、すべてのアプリの番号を合わせる。

    番号のファイルは 1 始まり (0 や空は「なし」) なので、a + 1 と b + 1 を入れ替える。
    """
    names = INDEX_FILES.get(shared_file, ())
    dirs = app_dirs()
    if not any(_same(d, APP_DIR) for d in dirs):
        dirs.append(APP_DIR)
    for folder in dirs:
        for name in names:
            path = folder / name
            text = read_text(path)
            try:
                n = int(text or "")
            except ValueError:
                continue
            if n == a + 1:
                n = b + 1
            elif n == b + 1:
                n = a + 1
            else:
                continue
            try:
                write_text(path, str(n))
            except OSError:
                pass


# ----------------------------------------------------------------------
# 初めて共有するとき: このアプリの設定を共有に移す (generation-standard 11 章)
# ----------------------------------------------------------------------
def _parse_tab_presets(text: Optional[str], count: int) -> list[tuple[str, str]]:
    """「番号 Tab 名前 Tab 値」の行を (名前, 値) の並び (count 件) にする。"""
    items = [("", "")] * count
    for line in (text or "").split("\n"):
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 3:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            continue
        if 1 <= n <= count:
            items[n - 1] = (parts[1].strip(), parts[2].strip().rstrip("/"))
    return items


def _format_tab_presets(items: list[tuple[str, str]]) -> str:
    return "".join(f"{i + 1}\t{name}\t{value}\r\n" for i, (name, value) in enumerate(items))


def _load_json_list(text: Optional[str]) -> Optional[list]:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _llm_key(item) -> Optional[tuple]:
    """エンハンサー・読み取りプリセットを比べる鍵 (空の枠は None)。"""
    if not isinstance(item, dict):
        return None
    system = str(item.get("system", "")).strip()
    template = str(item.get("template", "")).strip()
    if not system and not item.get("name"):
        return None
    try:
        temperature = round(float(item.get("temperature", 0.7)), 3)
    except (TypeError, ValueError):
        temperature = 0.7
    return (str(item.get("name", "")), system, template, temperature)


def _remap_index(name: str, mapping: dict[int, int]) -> None:
    """アプリの番号のファイル (1 始まり) を、移したあとの位置に付け替える。"""
    path = app_file(name)
    text = read_text(path)
    try:
        n = int(text or "")
    except ValueError:
        return
    if n - 1 in mapping and mapping[n - 1] != n - 1:
        try:
            write_text(path, str(mapping[n - 1] + 1))
        except OSError:
            pass


def _merge_tab(file: str, legacy: str, count: int, label: str, notes: list[str]) -> None:
    mine_text = read_text(app_file(file))
    if mine_text is None:
        old = read_text(app_file(legacy))
        if not old:
            return
        mine = [("", old.rstrip("/"))] + [("", "")] * (count - 1)  # プリセットができる前の単一の設定
    else:
        mine = _parse_tab_presets(mine_text, count)
    if not any(value for _name, value in mine):
        return
    shared_text = read_text(SHARED_DIR / file)
    if shared_text is None:
        write_text(SHARED_DIR / file, _format_tab_presets(mine))
        notes.append(f"{label}を共有に移しました")
        return
    shared = _parse_tab_presets(shared_text, count)
    mapping: dict[int, int] = {}
    added = overflow = 0
    for i, (name, value) in enumerate(mine):
        if not value:
            continue
        found = next((j for j, (_n, v) in enumerate(shared) if v and v.lower() == value.lower()), -1)
        if found < 0:
            found = next((j for j, (_n, v) in enumerate(shared) if not v), -1)
            if found < 0:
                overflow += 1
                continue
            shared[found] = (name, value)
            added += 1
        mapping[i] = found
    if added:
        write_text(SHARED_DIR / file, _format_tab_presets(shared))
        notes.append(f"{label}を {added} 件、共有に足しました")
    if overflow:
        notes.append(f"{label}は枠がいっぱいで {overflow} 件を共有に入れられませんでした")
    for index_name in INDEX_FILES.get(file, ()):
        _remap_index(index_name, mapping)


def _merge_llm_presets(file: str, count: int, label: str, notes: list[str]) -> None:
    mine = _load_json_list(read_text(app_file(file)))
    if not mine or not any(_llm_key(item) for item in mine):
        return
    shared_text = read_text(SHARED_DIR / file)
    shared = _load_json_list(shared_text)
    if shared is None:
        write_text(SHARED_DIR / file, json.dumps(mine[:count], ensure_ascii=False, indent=2))
        notes.append(f"{label}を共有に移しました")
        return
    shared = list(shared[:count]) + [{} for _ in range(count - len(shared))]
    mapping: dict[int, int] = {}
    added = overflow = 0
    for i, item in enumerate(mine[:count]):
        key = _llm_key(item)
        if key is None:
            continue
        found = next((j for j, s in enumerate(shared) if _llm_key(s) == key), -1)
        if found < 0:
            found = next((j for j, s in enumerate(shared) if _llm_key(s) is None), -1)
            if found < 0:
                overflow += 1
                continue
            shared[found] = item
            added += 1
        mapping[i] = found
    if added:
        write_text(SHARED_DIR / file, json.dumps(shared, ensure_ascii=False, indent=2))
        notes.append(f"{label}を {added} 件、共有に足しました")
    if overflow:
        notes.append(f"{label}は枠がいっぱいで {overflow} 件を共有に入れられませんでした")
    for index_name in INDEX_FILES.get(file, ()):
        _remap_index(index_name, mapping)


def _merge_single(file: str, mine: Optional[str], label: str, notes: list[str], differ: list[str]) -> None:
    """ベースプロンプトや LLM の接続: 共有が空のときだけ入れる。違っていれば共有を残して知らせる。"""
    if not mine:
        return
    shared = read_text(SHARED_DIR / file)
    if not shared:
        write_text(SHARED_DIR / file, mine)
        notes.append(f"{label}を共有に移しました")
    elif shared.strip() != mine.strip():
        differ.append(label)


def merge_into_shared(extra: Optional[dict[str, str]] = None) -> list[str]:
    """このアプリの設定を、一度だけ共有に移す。戻り値はステータスバーで知らせる文 (何もなければ空)。

    extra は、アプリのフォルダ以外に置いていた LLM の接続など ({共有のファイル名: 値})。
    アプリの古いファイルは消さない (共有に移したあとは読まない)。
    """
    from . import presets  # 枠の数 (循環 import を避ける)

    if app_file(MERGED_FILE).is_file():
        return []
    notes: list[str] = []
    differ: list[str] = []
    try:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        _merge_tab(CONNECTIONS_FILE, "comfy_url.txt", 10, "接続先", notes)
        _merge_tab(WORKFLOWS_FILE, "comfy_workflow.txt", 100, "ワークフロー", notes)
        old_base = read_text(app_file("comfy_base_prompt.txt"))
        _merge_single(BASE_VIDEO_FILE, read_text(app_file(BASE_VIDEO_FILE)) or old_base,
                      "ベースプロンプト (動画用)", notes, differ)
        _merge_single(BASE_IMAGE_FILE, read_text(app_file(BASE_IMAGE_FILE)) or old_base,
                      "ベースプロンプト (画像用)", notes, differ)
        _merge_llm_presets(ENHANCER_FILE, presets.PRESET_COUNT, "エンハンサー", notes)
        _merge_llm_presets(READER_FILE, presets.PRESET_COUNT, "読み取りプリセット", notes)
        values = {name: read_text(app_file(name)) for name in LLM_FILES}
        for name, value in (extra or {}).items():
            if value and not values.get(name):
                values[name] = value
        llm_notes: list[str] = []
        for name in LLM_FILES:
            _merge_single(name, values.get(name), "LLM の接続", llm_notes, differ)
        if llm_notes:
            notes.append("LLM の接続を共有に移しました")
        write_text(app_file(MERGED_FILE), "1")
    except OSError as exc:
        return [f"共有の設定に移せませんでした ({exc})。次に起動したときにもう一度試します"]
    differ = list(dict.fromkeys(differ))
    if differ:
        notes.append(f"{'・'.join(differ)}は共有のものと違うので、共有のほうを使います")
    return notes


def start(app_name: str, extra: Optional[dict[str, str]] = None) -> list[str]:
    """起動したときに呼ぶ: 置き場所を決め、共有に移し、apps.txt に名乗る。戻り値は知らせる文。"""
    configure(app_name)
    notes = merge_into_shared(extra)
    try:
        register_app()
    except OSError:
        pass
    return notes
