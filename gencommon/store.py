"""設定ファイルの置き場所。共有するものは %APPDATA%\\GenCommon、それ以外はアプリごとのフォルダ。

generation-standard.md の 8・11 章:
- 接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセット・LLM の接続は
  J-Rate・PromptWordCrafter・ImageMimicArt で共有する (どのアプリで変えても、ほかの 2 つに効く)。
- 使用中の番号・前回の値・セット・記録・ジョブはアプリごと。

ファイルの名前は共有でもアプリごとでも同じなので、呼び出し側は名前だけを渡せばよい
(file_path() が名前で置き場所を振り分ける)。

共有の置き場所は PC ごとに変えられる (generation-standard 11 章「別の PC と共有する」)。
%APPDATA%\\GenCommon\\shared_dir.txt にフォルダ (\\\\RAVEN\\SharBox\\GenCommon など) が書いてあれば、
共有するものはそこを読み書きし、%APPDATA%\\GenCommon は最後に読めた内容の写しになる。
繋がらないときは写しを読むだけにして、共有するものは書かない。

使用中・前回の番号 (アプリごと) は、別の PC で並べ替えられてもずれないよう、
指している項目 (URL・パス・名前) も selected_keys.txt に覚えておき、読むときに合わせ直す。

テストで差し替えられるよう、APP_DIR / SHARED_DIR / MIRROR_DIR はモジュール変数にしておく。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(os.environ.get("APPDATA") or Path.home())
SHARED_DIR = _ROOT / "GenCommon"  # 共有するものを読み書きする場所
MIRROR_DIR: Optional[Path] = None  # 共有が別の場所のとき、この PC の写し (%APPDATA%\GenCommon)
APP_NAME = ""
APP_DIR = _ROOT / "GenCommon-unconfigured"

LOCATION_FILE = "shared_dir.txt"  # 共有の置き場所 (この PC の %APPDATA%\GenCommon に置く。空・なし = ここ)
SELECTED_FILE = "selected_keys.txt"  # 番号のファイルが指している項目 (アプリごと。1 行に「ファイル名 Tab 項目」)

# 共有するファイル (generation-standard 8 章「共有」)
CONNECTIONS_FILE = "comfy_presets.txt"
WORKFLOWS_FILE = "comfy_workflows.txt"
BASE_VIDEO_FILE = "comfy_base_prompt_video.txt"
BASE_IMAGE_FILE = "comfy_base_prompt_image.txt"
ENHANCER_FILE = "enhancer_presets.json"
READER_FILE = "reader_presets.json"
LLM_FILES = ("llm_url.txt", "reader_model.txt", "enhance_model.txt", "reader_max_side.txt")
CONNECTION_FOLDERS_FILE = "comfy_connection_folders.txt"  # 接続先ごとのワークフローのフォルダ (1 行に「URL Tab フォルダ」)
APPS_FILE = "apps.txt"
SHARED_FILES = frozenset(
    (CONNECTIONS_FILE, WORKFLOWS_FILE, BASE_VIDEO_FILE, BASE_IMAGE_FILE, ENHANCER_FILE, READER_FILE,
     CONNECTION_FOLDERS_FILE)
    + LLM_FILES
)
# この PC の %APPDATA%\GenCommon に置くもの (共有の置き場所を変えても、PC ごと)
LOCAL_FILES = frozenset((APPS_FILE, LOCATION_FILE))

# 共有の一覧を指す、アプリごとの番号のファイル (上へ / 下へで入れ替えたときに直す)
INDEX_FILES = {
    CONNECTIONS_FILE: ("comfy_preset.txt",),
    WORKFLOWS_FILE: ("comfy_workflow_preset.txt",),
    ENHANCER_FILE: ("enhancer_generate.txt", "enhance_preset.txt"),
    READER_FILE: ("reader_generate.txt", "reader_preset.txt"),
}

_SHARED_OF_INDEX = {index: shared for shared, names in INDEX_FILES.items() for index in names}

MERGED_FILE = "gencommon_merged.txt"  # このアプリの設定を共有に移した印

_write_lock = threading.Lock()


def configure(app_name: str) -> None:
    """アプリの名前を決める (起動したら最初に呼ぶ)。アプリごとのファイルは %APPDATA%\\(アプリ名) に置く。"""
    global APP_NAME, APP_DIR
    APP_NAME = app_name
    APP_DIR = _ROOT / app_name
    _refresh_location(force=True)


def is_shared(name: str) -> bool:
    return name in SHARED_FILES


def local_dir() -> Path:
    """この PC の %APPDATA%\\GenCommon (apps.txt と shared_dir.txt、共有が別の場所のときは写し)。"""
    return MIRROR_DIR if MIRROR_DIR is not None else SHARED_DIR


def file_path(name: str) -> Path:
    if name in LOCAL_FILES:
        _refresh_location()
        return local_dir() / name
    if is_shared(name):
        return shared_dir() / name
    return APP_DIR / name


# ----------------------------------------------------------------------
# 共有の置き場所 (別の PC と共有するとき)
# ----------------------------------------------------------------------
_location_stamp: Optional[float] = -1.0  # shared_dir.txt の更新時刻 (なし = None。まだ見ていない = -1)
_online: Optional[bool] = None
_online_checked = 0.0
_ONLINE_RECHECK = 10.0  # 繋がっているとき、次に確かめるまでの秒数
_OFFLINE_RECHECK = 30.0  # 繋がらないとき (繋がらないフォルダを見ると何秒も待たされるので、間を空ける)


def location() -> Optional[Path]:
    """shared_dir.txt に書いてある共有の置き場所 (なければ None = この PC の %APPDATA%\\GenCommon)。"""
    _refresh_location()
    return SHARED_DIR if MIRROR_DIR is not None else None


def _refresh_location(force: bool = False) -> None:
    """shared_dir.txt が変わっていれば読み直す (ほかのアプリで場所を変えても、再起動せずに効くように)。"""
    global SHARED_DIR, MIRROR_DIR, _location_stamp, _online
    base = local_dir()
    pointer = base / LOCATION_FILE
    try:
        stamp: Optional[float] = pointer.stat().st_mtime
    except OSError:
        stamp = None
    if not force and stamp == _location_stamp:
        return
    _location_stamp = stamp
    text = read_text(pointer) if stamp is not None else None
    folder = Path(text.strip().strip('"')) if text and text.strip().strip('"') else None
    if folder is not None and not _same(folder, base):
        SHARED_DIR, MIRROR_DIR = folder, base
    else:
        SHARED_DIR, MIRROR_DIR = base, None
    _online = None


def online() -> bool:
    """共有の置き場所を読めるか (この PC の中なら、いつも True)。"""
    global _online, _online_checked
    _refresh_location()
    if MIRROR_DIR is None:
        return True
    now = time.monotonic()
    wait = _ONLINE_RECHECK if _online else _OFFLINE_RECHECK
    if _online is None or now - _online_checked >= wait:
        try:
            _online = SHARED_DIR.is_dir()
        except OSError:
            _online = False
        _online_checked = now
    return bool(_online)


def _set_offline() -> None:
    global _online, _online_checked
    _online = False
    _online_checked = time.monotonic()


def shared_dir() -> Path:
    """いま共有するものを読む場所 (別の場所に繋がらないときは、この PC の写し)。"""
    if MIRROR_DIR is not None and not online():
        return MIRROR_DIR
    return SHARED_DIR


def offline_message() -> str:
    """共有の置き場所に繋がらないときに画面に出す文 (繋がっていれば空)。"""
    if MIRROR_DIR is None or online():
        return ""
    return (f"共有の設定の置き場所 ({SHARED_DIR}) に繋がりません。この PC に残っている写しを表示しています。"
            "繋がるまで、接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセット・LLM の接続の変更は保存されません。")


def _read_raw(path: Path) -> Optional[str]:
    """ファイルの中身そのまま (ないときは None)。読めないときは OSError / UnicodeDecodeError。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def _load_shared(name: str) -> Optional[str]:
    """別の場所の共有を読み、この PC の写しも合わせる。読めなければ写しを読む。"""
    try:
        raw = _read_raw(SHARED_DIR / name)
    except (OSError, UnicodeDecodeError):
        if not SHARED_DIR.is_dir():
            _set_offline()
        return read_text(MIRROR_DIR / name)
    mirror = MIRROR_DIR / name
    try:
        current = _read_raw(mirror)
    except (OSError, UnicodeDecodeError):
        current = None
    try:
        if raw is None:
            if current is not None:
                mirror.unlink()
        elif raw != current:
            write_text(mirror, raw)
    except OSError:
        pass
    return None if raw is None else raw.strip()


def set_location(folder: str) -> list[str]:
    """共有の置き場所を変える (空ならこの PC の %APPDATA%\\GenCommon に戻す)。戻り値は知らせる文。

    新しい場所に移るときは、今の設定を新しい場所に足し (11 章の引き継ぎと同じ決まり)、
    この PC のアプリの番号を足した位置に付け替えてから、新しい場所の内容を写しにする。
    新しい場所を作れない・書けないときは OSError (場所は変えない)。
    """
    _refresh_location()
    base = local_dir()
    folder = folder.strip().strip('"')
    if not folder or _same(Path(folder), base):
        if MIRROR_DIR is not None and online():
            _copy_shared(SHARED_DIR, base)
        try:
            (base / LOCATION_FILE).unlink()
        except FileNotFoundError:
            pass
        _refresh_location(force=True)
        return [f"共有の設定の置き場所を、この PC ({base}) に戻しました"]

    target = Path(folder)
    if MIRROR_DIR is not None and _same(target, SHARED_DIR):
        return []
    target.mkdir(parents=True, exist_ok=True)
    source = shared_dir()
    index_dirs = app_dirs()
    if not any(_same(d, APP_DIR) for d in index_dirs):
        index_dirs.append(APP_DIR)
    _remember_all(index_dirs)
    notes: list[str] = []
    differ: list[str] = []
    _merge_everything(source, target, None, index_dirs, notes, differ)
    _copy_shared(target, base)
    write_text(base / LOCATION_FILE, str(target))
    _refresh_location(force=True)
    differ = list(dict.fromkeys(differ))
    if differ:
        notes.append(f"{'・'.join(differ)}は共有の置き場所にあったものと違うので、置き場所にあったほうを使います")
    return [f"共有の設定の置き場所を {target} にしました"] + notes


def _copy_shared(source: Path, target: Path) -> None:
    """共有するファイルを source から target に写す (source にないものは target からも消す)。"""
    for name in SHARED_FILES:
        try:
            raw = _read_raw(source / name)
        except (OSError, UnicodeDecodeError):
            continue
        try:
            if raw is None:
                (target / name).unlink(missing_ok=True)
            else:
                write_text(target / name, raw)
        except OSError:
            pass


# ----------------------------------------------------------------------
# 使用中・前回の番号を、指している項目でも覚えておく (別の PC で並べ替えられてもずれないように)
# ----------------------------------------------------------------------
def _key_text(value: str) -> str:
    return " ".join(str(value).split())


def _item_keys(shared_file: str) -> list[str]:
    """共有の一覧の項目を見分ける文字列 (空の枠は "")。接続先・ワークフローは URL・パス、プリセットは名前。"""
    text = load(shared_file)
    if shared_file in (CONNECTIONS_FILE, WORKFLOWS_FILE):
        count = 10 if shared_file == CONNECTIONS_FILE else 100
        return [_key_text(value).lower() for _name, value in _parse_tab_presets(text, count)]
    keys = []
    for item in _load_json_list(text) or []:
        if not isinstance(item, dict):
            keys.append("")
            continue
        name = _key_text(item.get("name", ""))
        system = _key_text(item.get("system", ""))[:200]
        keys.append(name if name else (f"system:{system}" if system else ""))
    return keys


def _selected_keys(folder: Optional[Path] = None) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in (read_text((folder or APP_DIR) / SELECTED_FILE) or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip():
            keys[parts[0].strip()] = parts[1].strip()
    return keys


def _remember_key(index_file: str, key: str, folder: Optional[Path] = None) -> None:
    folder = folder or APP_DIR
    keys = _selected_keys(folder)
    if keys.get(index_file) == key:
        return
    if key:
        keys[index_file] = key
    else:
        keys.pop(index_file, None)
    try:
        write_text(folder / SELECTED_FILE, "".join(f"{k}\t{v}\r\n" for k, v in sorted(keys.items())))
    except OSError:
        pass


def _remember_all(folders: list[Path]) -> None:
    """アプリの番号が指している項目を、まだ覚えていなければ覚える (ほかの PC で並べ替えられる前に)。"""
    lists = {shared: _item_keys(shared) for shared in INDEX_FILES}
    for folder in folders:
        known = _selected_keys(folder)
        for index_file, shared in _SHARED_OF_INDEX.items():
            if index_file in known:
                continue
            key = _key_at(lists[shared], read_text(folder / index_file))
            if key:
                _remember_key(index_file, key, folder)


def _key_at(keys: list[str], text: Optional[str]) -> str:
    try:
        n = int(text or "")
    except ValueError:
        return ""
    return keys[n - 1] if 1 <= n <= len(keys) else ""


def _resolve_index(index_file: str, text: Optional[str]) -> Optional[str]:
    """番号のファイルの値を、覚えている項目の今の位置に合わせる (合わせたら書き直す)。"""
    try:
        n = int(text or "")
    except ValueError:
        return text
    if n < 1:
        return text
    keys = _item_keys(_SHARED_OF_INDEX[index_file])
    current = keys[n - 1] if n <= len(keys) else ""
    stored = _selected_keys().get(index_file)
    if stored and stored != current:
        found = next((i for i, k in enumerate(keys) if k == stored), -1)
        if found >= 0:
            try:
                write_text(APP_DIR / index_file, str(found + 1))
            except OSError:
                pass
            return str(found + 1)
    # 覚えていない (前の版で選んだ) / 覚えている項目がなくなった (書き換えられた) ときは、今の位置の項目を覚える
    if current and stored != current:
        _remember_key(index_file, current)
    return text


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
        # 共有の置き場所は別の PC からも書くので、PC の名前も付けて別名がぶつからないようにする
        host = os.environ.get("COMPUTERNAME", "")
        temp = path.with_name(f"{path.name}.{host}.{os.getpid()}.tmp")
        temp.write_text(value, encoding="utf-8", newline="")
        temp.replace(path)


def load(name: str) -> Optional[str]:
    if is_shared(name):
        _refresh_location()
        if MIRROR_DIR is not None:
            return _load_shared(name) if online() else read_text(MIRROR_DIR / name)
    text = read_text(file_path(name))
    if name in _SHARED_OF_INDEX:
        text = _resolve_index(name, text)
    return text


def save(name: str, value: str) -> None:
    """書く。共有の置き場所に繋がらないときは、共有するものは書かない (offline_message() で知らせる)。"""
    if is_shared(name):
        _refresh_location()
        if MIRROR_DIR is not None:
            if not online():
                return
            try:
                write_text(SHARED_DIR / name, value)
            except OSError:
                if not SHARED_DIR.is_dir():
                    _set_offline()
                return
            try:
                write_text(MIRROR_DIR / name, value)
            except OSError:
                pass
            return
    try:
        write_text(file_path(name), value)
    except OSError:
        pass
    if name in _SHARED_OF_INDEX:
        _remember_key(name, _key_at(_item_keys(_SHARED_OF_INDEX[name]), value))


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


def _remap_index(path: Path, mapping: dict[int, int]) -> None:
    """アプリの番号のファイル (1 始まり) を、移したあとの位置に付け替える。"""
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


def _remap_indexes(file: str, mapping: dict[int, int], index_dirs: list[Path]) -> None:
    for folder in index_dirs:
        for index_name in INDEX_FILES.get(file, ()):
            _remap_index(folder / index_name, mapping)


def _merge_tab(file: str, legacy: str, count: int, label: str, notes: list[str],
               src: Path, dst: Path, index_dirs: list[Path]) -> None:
    mine_text = read_text(src / file)
    if mine_text is None:
        old = read_text(src / legacy)
        if not old:
            return
        mine = [("", old.rstrip("/"))] + [("", "")] * (count - 1)  # プリセットができる前の単一の設定
    else:
        mine = _parse_tab_presets(mine_text, count)
    if not any(value for _name, value in mine):
        return
    shared_text = read_text(dst / file)
    if shared_text is None:
        write_text(dst / file, _format_tab_presets(mine))
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
        write_text(dst / file, _format_tab_presets(shared))
        notes.append(f"{label}を {added} 件、共有に足しました")
    if overflow:
        notes.append(f"{label}は枠がいっぱいで {overflow} 件を共有に入れられませんでした")
    _remap_indexes(file, mapping, index_dirs)


def _merge_llm_presets(file: str, count: int, label: str, notes: list[str],
                       src: Path, dst: Path, index_dirs: list[Path]) -> None:
    mine = _load_json_list(read_text(src / file))
    if not mine or not any(_llm_key(item) for item in mine):
        return
    shared_text = read_text(dst / file)
    shared = _load_json_list(shared_text)
    if shared is None:
        write_text(dst / file, json.dumps(mine[:count], ensure_ascii=False, indent=2))
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
        write_text(dst / file, json.dumps(shared, ensure_ascii=False, indent=2))
        notes.append(f"{label}を {added} 件、共有に足しました")
    if overflow:
        notes.append(f"{label}は枠がいっぱいで {overflow} 件を共有に入れられませんでした")
    _remap_indexes(file, mapping, index_dirs)


def _merge_single(file: str, mine: Optional[str], label: str, notes: list[str], differ: list[str],
                  dst: Path) -> None:
    """ベースプロンプトや LLM の接続: 共有が空のときだけ入れる。違っていれば共有を残して知らせる。"""
    if not mine:
        return
    shared = read_text(dst / file)
    if not shared:
        write_text(dst / file, mine)
        notes.append(f"{label}を共有に移しました")
    elif shared.strip() != mine.strip():
        differ.append(label)


def _merge_everything(src: Path, dst: Path, extra: Optional[dict[str, str]], index_dirs: list[Path],
                      notes: list[str], differ: list[str]) -> None:
    """src の設定を dst (共有) に足す。番号は index_dirs のフォルダのものを付け替える。失敗したら OSError。

    アプリの設定を初めて共有に移すとき (src = アプリのフォルダ) と、
    共有の置き場所を変えるとき (src = 今の共有) の両方で使う。
    """
    from . import presets  # 枠の数 (循環 import を避ける)

    dst.mkdir(parents=True, exist_ok=True)
    _merge_tab(CONNECTIONS_FILE, "comfy_url.txt", 10, "接続先", notes, src, dst, index_dirs)
    _merge_tab(WORKFLOWS_FILE, "comfy_workflow.txt", 100, "ワークフロー", notes, src, dst, index_dirs)
    old_base = read_text(src / "comfy_base_prompt.txt")
    _merge_single(BASE_VIDEO_FILE, read_text(src / BASE_VIDEO_FILE) or old_base,
                  "ベースプロンプト (動画用)", notes, differ, dst)
    _merge_single(BASE_IMAGE_FILE, read_text(src / BASE_IMAGE_FILE) or old_base,
                  "ベースプロンプト (画像用)", notes, differ, dst)
    _merge_llm_presets(ENHANCER_FILE, presets.PRESET_COUNT, "エンハンサー", notes, src, dst, index_dirs)
    _merge_llm_presets(READER_FILE, presets.PRESET_COUNT, "読み取りプリセット", notes, src, dst, index_dirs)
    values = {name: read_text(src / name) for name in LLM_FILES}
    for name, value in (extra or {}).items():
        if value and not values.get(name):
            values[name] = value
    llm_notes: list[str] = []
    for name in LLM_FILES:
        _merge_single(name, values.get(name), "LLM の接続", llm_notes, differ, dst)
    if llm_notes:
        notes.append("LLM の接続を共有に移しました")
    _merge_lines(CONNECTION_FOLDERS_FILE, src, dst)


def _merge_lines(file: str, src: Path, dst: Path) -> None:
    """「鍵 Tab 値」の行: dst にない鍵の行だけ足す (同じ鍵は dst のほうを残す)。"""
    mine = read_text(src / file)
    if not mine:
        return
    shared = read_text(dst / file) or ""
    keys = {line.split("\t", 1)[0].strip().lower() for line in shared.splitlines() if "\t" in line}
    added = [line for line in mine.splitlines() if "\t" in line and line.split("\t", 1)[0].strip().lower() not in keys]
    if added:
        lines = [line for line in shared.splitlines() if line.strip()] + added
        write_text(dst / file, "".join(f"{line}\r\n" for line in lines))


def merge_into_shared(extra: Optional[dict[str, str]] = None) -> list[str]:
    """このアプリの設定を、一度だけ共有に移す。戻り値はステータスバーで知らせる文 (何もなければ空)。

    extra は、アプリのフォルダ以外に置いていた LLM の接続など ({共有のファイル名: 値})。
    アプリの古いファイルは消さない (共有に移したあとは読まない)。
    共有の置き場所に繋がらないときは何もしない (次に起動したときに試す)。
    """
    if app_file(MERGED_FILE).is_file():
        return []
    if not online():
        return []
    notes: list[str] = []
    differ: list[str] = []
    try:
        _merge_everything(APP_DIR, SHARED_DIR, extra, [APP_DIR], notes, differ)
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
    _remember_all([APP_DIR])
    return notes
