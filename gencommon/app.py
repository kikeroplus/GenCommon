"""アプリごとに違うところ (generation-standard 11 章「アプリごとに違うところ」)。

共通の部品はここに入れた Profile を見て、文言・出力の置き場所・読み取りの有無を切り替える。
アプリは起動したときに setup() を 1 回呼ぶ。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import store


@dataclass
class Colors:
    ok: str = "#2e7d32"
    error: str = "#b71c1c"
    hint: str = "#666666"
    paused: str = "#e65100"  # ジョブ一覧の一時停止中の表示
    separator: str = "#cccccc"


@dataclass
class Profile:
    name: str  # アプリの名前 (%APPDATA% のフォルダ名、パラメーターのコピーの目印)
    reads: bool  # 読み取りがあるか (元画像から説明プロンプトを作る)
    source_word: str  # 生成の元の呼び名 (「テキストファイル」「元画像」)
    unit: str  # 数えるときの単位 (「件」「枚」)
    prompt_word: str  # プロンプトの呼び名 (「本文」「説明プロンプト」)
    prompt_where: str  # プロンプトを直す場所 (「編集欄」「右の枠」)
    column: str  # ジョブ一覧・生成時の設定で、元のファイルの欄の見出し (「ファイル」「元画像」)
    # 出来たファイルの保存先 (重なりを避ける前の名前): (元のファイル, 頼んだ内容, 動画か, 拡張子) → パス
    output_path: Callable[[Path, object, bool, str], Path] = None
    workflow_hint: str = ""  # 「生成の設定」のワークフローのタブに出す説明
    output_folder: bool = False  # 「生成の設定」に出力フォルダの欄を出す
    output_hint: str = ""
    open_result_label: str = "生成が終わったら、出来たファイルを開く (まとめて生成のときは開きません)"
    open_on_done: bool = False  # 1 件の生成が終わったら、出来たファイルを関連付けアプリで開く (オプションがオンのとき)
    skip_empty: bool = False  # プロンプトが空のものは生成しない (まとめてでは除き、1 件では押せなくする)
    colors: Colors = field(default_factory=Colors)


PROFILE: Optional[Profile] = None


def setup(profile: Profile, extra_llm: Optional[dict[str, str]] = None) -> list[str]:
    """アプリの起動時に呼ぶ。設定の置き場所を決め、初めてなら共有に移す。戻り値は知らせる文。"""
    global PROFILE
    PROFILE = profile
    return store.start(profile.name, extra_llm)


def profile() -> Profile:
    if PROFILE is None:
        raise RuntimeError("gencommon.app.setup() を先に呼んでください")
    return PROFILE
