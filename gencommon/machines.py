"""ComfyUI の台の振り分け (PrismLab の「振り分け (台)」と同じ考え。generation-standard 6 章「台に振り分ける」)。

- 登録してある接続先から最大 3 台を選ぶ (「台の設定...」)。役割は決めず、空いた台に何でも回す。
- 生成の画面の接続先で「振り分け」を選ぶと、ジョブを空いている台に回す (1 台に同時に 2 件まで)。
- 台はアプリごと (%APPDATA%\\(アプリ名)。2026-11-22 利用者の希望: 「PWC は台 A・B だけ」のように分ける)。
  別の PC とは共有しない。初めて開いたときは、前の置き場所 (%APPDATA%\\GenCommon) か PrismLab で選んだ台を写す。
  PrismLab のレシピで生成するとき (recipe_link.py) も、この台を PrismLab に渡す。
- 「使う」を外した台は休み (台の設定に残したまま、一時的に使わない)。

台の状態 (繋がらない・続けて失敗したので外した) は generation.GenerationController が持つ。
"""

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from . import app, comfy, store

MAX_MACHINES = 3
MACHINES_FILE = "comfy_machines.txt"  # 台 (1 行に 1 つ、接続先の URL)
MACHINES_OFF_FILE = "comfy_machines_off.txt"  # 休みの台 (1 行に 1 つ)
USE_FILE = "comfy_use_machines.txt"  # 生成の画面で、前回「振り分け」を選んでいたか (アプリごと)
PRISMLAB_MACHINES = "machines.txt"  # 初めて開いたときに写す、PrismLab の台
PRISMLAB_MACHINES_OFF = "machines_off.txt"

MACHINES_URL = "machines:"  # 生成の画面の接続先で「振り分け」を選んだときの URL の目印 (ジョブは番が来たときに台が決まる)
LABEL = "振り分け"


def is_machines(url: str) -> bool:
    return (url or "").strip().lower() == MACHINES_URL


def _parse(text: Optional[str]) -> list[str]:
    urls: list[str] = []
    for line in (text or "").splitlines():
        url = line.strip().rstrip("/")
        if url and url.lower() not in [u.lower() for u in urls]:
            urls.append(url)
    return urls[:MAX_MACHINES]


def _seed() -> None:
    """まだ台を決めていなければ、前の置き場所 (この PC で共通だったころ) か PrismLab で選んだ台を写す (1 回だけ)。"""
    path = store.app_file(MACHINES_FILE)
    if path.exists():
        return
    old = store.local_dir()
    if (old / MACHINES_FILE).exists():
        urls = _parse(store.read_text(old / MACHINES_FILE))
        off = {u.lower() for u in _parse(store.read_text(old / MACHINES_OFF_FILE))}
    else:
        prism = store._ROOT / "PrismLab"
        urls = _parse(store.read_text(prism / PRISMLAB_MACHINES))
        off = {u.lower() for u in _parse(store.read_text(prism / PRISMLAB_MACHINES_OFF))}
    try:
        store.write_text(path, "\n".join(urls))
        store.write_text(store.app_file(MACHINES_OFF_FILE), "\n".join(u for u in urls if u.lower() in off))
    except OSError:
        pass


def all_machines() -> list[str]:
    """台の設定で選んである台の URL (休みの台も入れる。最大 3 つ)。"""
    _seed()
    return _parse(store.read_text(store.app_file(MACHINES_FILE)))


def machines_off() -> set[str]:
    """休みの台の URL の小文字。"""
    return {u.lower() for u in _parse(store.read_text(store.app_file(MACHINES_OFF_FILE)))}


def machines() -> list[str]:
    """使う台の URL (休みの台を除く)。"""
    off = machines_off()
    return [u for u in all_machines() if u.lower() not in off]


EDITS = [0]  # 台の設定を OK で閉じた回数 (生成ジョブが見て、続けて失敗したので外した台を戻す)


def set_machines(urls: list[str], off: set) -> None:
    EDITS[0] += 1
    urls = [u.strip().rstrip("/") for u in urls[:MAX_MACHINES]]
    off = {u.lower() for u in off}
    try:
        store.write_text(store.app_file(MACHINES_FILE), "\n".join(urls))
        store.write_text(store.app_file(MACHINES_OFF_FILE), "\n".join(u for u in urls if u.lower() in off))
    except OSError:
        pass


def name_of(url: str) -> str:
    key = (url or "").rstrip("/").lower()
    for p in comfy.connections():
        if not p.is_empty and p.value.rstrip("/").lower() == key:
            return p.name or p.value
    return url


def summary() -> str:
    """生成の画面の接続先に出す「振り分け (台: A · B)」。"""
    off = machines_off()
    on = [name_of(u) for u in all_machines() if u.lower() not in off]
    resting = [name_of(u) for u in all_machines() if u.lower() in off]
    if not on and not resting:
        return f"{LABEL} (台が決まっていません。「台の設定...」で選んでください)"
    text = f"{LABEL} (台: {' · '.join(on) if on else 'なし'})"
    if resting:
        text += f"  (休み: {' · '.join(resting)})"
    return text


def no_machines_reason() -> str:
    """使う台がないときの知らせ (あれば空)。"""
    if machines():
        return ""
    if all_machines():
        return "振り分ける台がありません (どの台も休みです。「台の設定...」で「使う」にチェックを入れてください)。"
    return "振り分ける台が決まっていません (「台の設定...」で選んでください)。"


def load_use() -> bool:
    return (store.read_text(store.app_file(USE_FILE)) or "") == "1"


def save_use(use: bool) -> None:
    try:
        store.write_text(store.app_file(USE_FILE), "1" if use else "0")
    except OSError:
        pass


def use_label(on: bool) -> str:
    """「使う」チェックの文字。外しても「使う」のままだと紛らわしいので、今の状態を出す (PrismLab と同じ)。"""
    return "使う" if on else "使わない"


class MachinesDialog(QDialog):
    """台の設定: 登録してある接続先から 3 台まで選ぶ (PrismLab の「台の設定」と同じ画面)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("台の設定")
        self.resize(480, 0)
        box = QVBoxLayout(self)
        hint = QLabel(
            f"{app.profile().name} で使う ComfyUI を 3 台まで選びます (PrismLab のレシピで生成するときも、この台を使います)。"
            "役割は決めず、空いた台に何でも回します (1 台に同時に 2 件まで。使っている数の少ない台が先)。"
            "どの台も同じワークフローを使うので、ワークフローが使うモデルはどの台にも同じ名前で置いてください "
            "(台用のフォルダに置いたワークフローは、その台にだけ送ります)。"
            "「使う」を外した台は休み (設定に残したまま一時的に使わない) です。"
            "台はアプリごとに選びます (ほかのアプリとは別)。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {app.profile().colors.hint};")
        box.addWidget(hint)
        form = QFormLayout()
        box.addLayout(form)
        urls, off = all_machines(), machines_off()
        self.boxes: list[QComboBox] = []
        self.uses: list[QCheckBox] = []
        for n in range(MAX_MACHINES):
            combo = QComboBox()
            on = not (n < len(urls) and urls[n].lower() in off)
            use = QCheckBox(use_label(on))
            use.setChecked(on)
            use.toggled.connect(lambda checked, b=use: b.setText(use_label(checked)))
            row = QHBoxLayout()
            row.addWidget(combo, 1)
            row.addWidget(use)
            form.addRow(f"台 {n + 1}", row)
            self.boxes.append(combo)
            self.uses.append(use)
        edit = QPushButton("接続先を登録する (生成の設定)...")
        edit.clicked.connect(self._edit)
        box.addWidget(edit)
        self.test = QPushButton("接続テスト")
        self.test.clicked.connect(self._test)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.test)
        row.addWidget(self.result, 1)
        box.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self._fill(urls)

    def _fill(self, urls: list[str]) -> None:
        conns = [(p.label(i), p.value.rstrip("/")) for i, p in enumerate(comfy.connections()) if not p.is_empty]
        for n, combo in enumerate(self.boxes):
            combo.clear()
            combo.addItem("(使わない)", "")
            for label, url in conns:
                combo.addItem(label, url)
            want = urls[n] if n < len(urls) else ""
            index = next((i for i in range(combo.count()) if str(combo.itemData(i)).lower() == want.lower()), -1)
            if index < 0 and want:
                combo.addItem(f"(登録にない) {want}", want)
                index = combo.count() - 1
            combo.setCurrentIndex(max(index, 0))

    def urls(self) -> list[str]:
        result: list[str] = []
        for combo in self.boxes:
            url = str(combo.currentData() or "")
            if url and url.lower() not in [u.lower() for u in result]:
                result.append(url)
        return result

    def off(self) -> set:
        return {str(c.currentData()) for c, u in zip(self.boxes, self.uses) if c.currentData() and not u.isChecked()}

    def _edit(self) -> None:
        from .comfy_dialogs import ComfySettingsDialog  # comfy_dialogs がこのモジュールを読むので、ここで読む

        current = self.urls()
        ComfySettingsDialog(self, "接続先").exec()
        self._fill(current)

    def _test(self) -> None:
        lines = []
        off = {u.lower() for u in self.off()}
        for url in self.urls():
            if url.lower() in off:
                lines.append(f"{url}: 休み")
                continue
            error = comfy.test_connection(url)
            lines.append(f"{url}: " + ("OK" if error is None else "繋がりません"))
        self.result.setText("\n".join(lines) or "台が選ばれていません。")

    def _save(self) -> None:
        set_machines(self.urls(), self.off())
        self.accept()
