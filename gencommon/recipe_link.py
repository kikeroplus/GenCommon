"""PrismLab のレシピで生成する (PrismLab の docs\\spec.md 6 章。2026-11-22 利用者の決定)。

生成の設定はすべて PrismLab で行い、PrismLab が「ほかのアプリに出す」にしたレシピだけを、ファイルを選んで押すだけで生成する
(「選んでポン」)。PrismLab の画面は出さない: PrismLab が動いていなければ「PrismLab.exe --background」で裏で起動し、
パイプ (PrismLab-(ユーザー名)) に {"cmd": "run", ...} を送る。生成は PrismLab が動かす (台の空きを 1 か所で数えるため)。

- 出してもらったレシピ: %APPDATA%\\PrismLab\\published.json (PrismLab がレシピを保存するたびに書く)
- 進み具合: %APPDATA%\\PrismLab\\external_status.json (PrismLab が書く。こちらは読むだけ)
- 台: このアプリで選んだ台 (machines.py) を頼むときに渡す。台の設定を OK で閉じたら {"cmd": "machines", ...} を送り、
  動いている頼みの台も入れ替えてもらう
- 出来たファイルはこのアプリの決まりの場所 (テキストのフォルダの「生成結果」) に、PrismLab が置く。記録も PrismLab に残り、
  生成結果の帯はそれを読む (どのレシピの何番目のステップで出来たかも見られる)
"""

import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtNetwork import QLocalSocket

from . import app, machines

PRISMLAB = "PrismLab"
SERVER_NAME = f"PrismLab-{getpass.getuser()}"
SEND_ARG = "--send"
BACKGROUND_ARG = "--background"
PUBLISHED_FILE = "published.json"
STATUS_FILE = "external_status.json"


def _prism_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / PRISMLAB


def _read_json(path: Path):
    """読めなければ None。PrismLab が書き換えている瞬間に当たったときのため、少し待ってもう一度だけ読む。"""
    for attempt in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == 0:
                time.sleep(0.05)
    return None


@dataclass
class PublishedRecipe:
    name: str
    index: int
    flow: str = ""  # 「読み取り → 盛る → 生成 (wan) 🎬」
    repeat: int = 1
    problem: str = ""  # このアプリでは使えない理由 (入口が合わないなど)


def recipes(app_name: str = "") -> list[PublishedRecipe]:
    """このアプリに出してもらったレシピ (PrismLab の並び順)。"""
    app_name = app_name or app.profile().name
    data = _read_json(_prism_dir() / PUBLISHED_FILE)
    items = data.get("recipes") if isinstance(data, dict) else None
    result = []
    for d in items or []:
        if not isinstance(d, dict) or app_name not in (d.get("apps") or []):
            continue
        try:
            index = int(d.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        problems = d.get("problems") if isinstance(d.get("problems"), dict) else {}
        result.append(PublishedRecipe(str(d.get("name") or ""), index, str(d.get("flow") or ""),
                                      int(d.get("repeat") or 1), str(problems.get(app_name) or "")))
    return result


def published_stamp() -> float:
    """published.json の更新時刻 (変わったら一覧を作り直す)。"""
    try:
        return (_prism_dir() / PUBLISHED_FILE).stat().st_mtime
    except OSError:
        return 0.0


def find_exe() -> Optional[list[str]]:
    """PrismLab を起動するコマンド (デスクトップの Application、なければ隣の PrismLab の dist、開発中は main.py)。"""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    candidates = [Path.home() / "Desktop" / "Application" / "PrismLab.exe"]
    for parent in here.parents:
        candidates.append(parent / "PrismLab" / "dist" / "PrismLab.exe")
    exe = next((p for p in candidates if p.is_file()), None)
    if exe is not None:
        return [str(exe)]
    if not getattr(sys, "frozen", False):
        for parent in here.parents:
            script = parent / "PrismLab" / "main.py"
            if script.is_file():
                return [sys.executable, str(script)]
    return None


def _send(message: dict) -> str:
    """PrismLab に送る (動いていなければ裏で起動して渡す)。うまくいけば空、だめなら理由。"""
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(700):
        sock.write(data + b"\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        if not sock.waitForDisconnected(3000):
            sock.abort()
            return "PrismLab が受け取りませんでした (応答がありません)。"
        return ""
    if message.get("cmd") != "run":
        return "PrismLab が動いていません。"
    command = find_exe()
    if command is None:
        return "PrismLab が見つかりません (デスクトップの Application に PrismLab.exe を置いてください)。"
    try:
        fd, temp = tempfile.mkstemp(prefix="prismlab_run_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        subprocess.Popen(command + [BACKGROUND_ARG, SEND_ARG, temp])  # noqa: S603 - 決まった PrismLab に渡す
    except OSError as exc:
        return f"PrismLab を起動できません: {exc}"
    return ""


def run(recipe: PublishedRecipe, files: list[Path]) -> tuple[str, str]:
    """レシピで生成するよう頼む。(頼みの番号, だめなら理由)。"""
    if recipe.problem:
        return "", recipe.problem
    urls = machines.machines()
    if not urls:
        return "", machines.no_machines_reason().replace("振り分ける", "使う")
    rid = f"{app.profile().name[:3]}-{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    error = _send({"cmd": "run", "id": rid, "from": app.profile().name, "recipe": recipe.name, "index": recipe.index,
                   "files": [str(f) for f in files], "machines": urls})
    return ("", error) if error else (rid, "")


def update_machines() -> str:
    """台の設定を変えた: 動いている頼みの台も入れ替えてもらう (2026-11-23 利用者の希望)。
    PrismLab が動いていなければ何もしない (起動もしない。動いている頼みはない)。"""
    error = _send({"cmd": "machines", "from": app.profile().name, "machines": machines.machines()})
    return "" if error == "PrismLab が動いていません。" else error


def cancel(rid: str) -> str:
    return _send({"cmd": "run_cancel", "id": rid})


def open_prismlab() -> str:
    """PrismLab の画面を出す (レシピを直すとき)。"""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(700):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return ""
    command = find_exe()
    if command is None:
        return "PrismLab が見つかりません。"
    try:
        subprocess.Popen(command)  # noqa: S603
    except OSError as exc:
        return f"PrismLab を起動できません: {exc}"
    return ""


@dataclass
class Status:
    """頼んだ生成 1 件の進み具合 (PrismLab が書いたもの)。"""

    id: str
    recipe: str = ""
    state: str = "running"  # running / done / canceled / error
    error: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    canceled: int = 0
    running: int = 0
    waiting: int = 0
    gated: int = 0
    outputs: int = 0
    created: float = 0.0
    finished_at: Optional[float] = None
    machines: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    paths: list = field(default_factory=list)  # 出来たファイル
    untranslated: int = 0  # 「英語に」で LLM に繋がらず、訳さずに送った件
    warning: str = ""  # 知らせ (失敗ではない。「LLM に繋がらないので、英語にせずに送りました (n 件)」)

    @property
    def active(self) -> bool:
        return self.state == "running"


def statuses(app_name: str = "") -> dict[str, Status]:
    """このアプリが頼んだものの進み具合 (番号 → Status)。"""
    app_name = app_name or app.profile().name
    data = _read_json(_prism_dir() / STATUS_FILE)
    result = {}
    for d in (data.get("requests") if isinstance(data, dict) else None) or []:
        if not isinstance(d, dict) or d.get("app") != app_name or not d.get("id"):
            continue
        fields = Status.__dataclass_fields__
        try:
            st = Status(**{k: v for k, v in d.items() if k in fields})
        except TypeError:
            continue
        result[st.id] = st
    return result

