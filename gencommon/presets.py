"""LLM に頼むときのプリセット (名前・指示文・送る文・temperature)。読み取りとエンハンサーで同じ作り。

登録は 3 つのアプリで共有する (%APPDATA%\\GenCommon)。
「プリセットを保存」「削除」「▲ 上へ」「▼ 下へ」でその場で書き込む (キャンセルしても残る)。
書くときはファイルを読み直してから、その枠だけを変える (ほかのアプリで足したものを消さない)。
"""

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import llm, store

PRESET_COUNT = 20
PROMPT_MARK = "{prompt}"  # 送る文の雛形で、元の文に置き換わる目印 (エンハンサー)
READ_TEMPLATE = "Write a text-to-image prompt that recreates this image."  # 読み取りで画像と一緒に送る既定の文
DEFAULT_TEMPLATES = ("", PROMPT_MARK, READ_TEMPLATE)  # 送る文がこれだけで指示文がなければ未登録
DEFAULT_TEMPERATURE = 0.7

ICON_COLOR = "#b8b8b8"
COPIED_COLOR = "#66bb6a"
COPIED_MS = 1500  # コピーしたあと ✓ を出しておく時間


@dataclass
class LlmPreset:
    name: str = ""
    system: str = ""
    template: str = ""
    temperature: float = DEFAULT_TEMPERATURE

    @property
    def is_empty(self) -> bool:
        return not self.system.strip() and self.template.strip() in DEFAULT_TEMPLATES

    def label(self, index: int) -> str:
        if self.is_empty:
            text = "(未登録)"
        else:
            text = self.name or "(名前なし)"
        return f"{index + 1}: {text}"


def load_presets(file_name: str, examples: list[LlmPreset], default_template: str) -> list[LlmPreset]:
    """file_name (共有の置き場所) を読む。まだなければ 1 番から例を入れておく。"""
    presets = [LlmPreset(template=default_template) for _ in range(PRESET_COUNT)]
    text = store.load(file_name)
    if text is None:
        for i, example in enumerate(examples):
            presets[i] = LlmPreset(**example.__dict__)
        return presets
    try:
        items = json.loads(text)
    except ValueError:
        return presets
    if not isinstance(items, list):
        return presets
    for i, item in enumerate(items[:PRESET_COUNT]):
        if not isinstance(item, dict):
            continue
        presets[i] = LlmPreset(
            name=str(item.get("name", "")),
            system=str(item.get("system", "")),
            template=str(item.get("template", default_template)),
            temperature=_temperature(item.get("temperature")),
        )
    return presets


def save_presets(file_name: str, presets: list[LlmPreset]) -> None:
    data = [p.__dict__ for p in presets[:PRESET_COUNT]]
    store.save(file_name, json.dumps(data, ensure_ascii=False, indent=2))


def load_index(file_name: str) -> int:
    """前回選んだ番号 (0 始まり。なし / 読めなければ -1)。ファイルには 1 始まり (0 = なし) で書く。"""
    try:
        n = int(store.load(file_name) or "")
    except ValueError:
        return -1
    return n - 1 if 1 <= n <= PRESET_COUNT else -1


def save_index(file_name: str, index: int) -> None:
    store.save(file_name, str(index + 1 if index >= 0 else 0))


def snapshot(preset: LlmPreset, base_url: str, model: str, **extra) -> dict:
    """ジョブに持たせる、登録した時点のプリセットの写し (あとで設定を変えても、頼んだときの内容で)。"""
    return {**preset.__dict__, "url": base_url, "model": model, **extra}


def from_snapshot(snap: dict, default_template: str) -> LlmPreset:
    return LlmPreset(str(snap.get("name", "")), str(snap.get("system", "")),
                     str(snap.get("template", default_template)), _temperature(snap.get("temperature")))


def _temperature(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_TEMPERATURE


# ----------------------------------------------------------------------
# 返事の後始末
# ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```$", re.DOTALL)
_QUOTES = ('"', "“", "「")
_CLOSE_QUOTES = {'"': '"', "“": "”", "「": "」"}


def clean(text: str) -> str:
    """指示しても付けてくることがある、全体を囲むコードブロックや引用符を外す。"""
    text = text.strip()
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    for open_quote in _QUOTES:
        close_quote = _CLOSE_QUOTES[open_quote]
        if len(text) >= 2 and text.startswith(open_quote) and text.endswith(close_quote):
            inner = text[1:-1]
            if open_quote not in inner and close_quote not in inner:
                text = inner.strip()
            break
    return text


# ----------------------------------------------------------------------
# アイコン (コピー / コピーした)
# ----------------------------------------------------------------------
def _make_icon(draw) -> QIcon:
    """16px 相当の線画アイコン (高 DPI でもにじまないよう 2 倍で描く)。"""
    icon = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(16 * scale, 16 * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(scale, scale)
        draw(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def copy_icon() -> QIcon:
    """2 枚の紙が重なったコピーのアイコン。"""

    def draw(painter: QPainter):
        painter.setPen(QPen(QColor(ICON_COLOR), 1.3))
        back = QPainterPath()  # 後ろの紙は、前の紙に隠れる部分を描かない
        back.moveTo(3.5, 10.5)
        back.lineTo(3.5, 3.5)
        back.quadTo(3.5, 2.5, 4.5, 2.5)
        back.lineTo(10.5, 2.5)
        painter.drawPath(back)
        painter.drawRoundedRect(QRectF(5.5, 4.5, 7.5, 9), 1.2, 1.2)

    return _make_icon(draw)


def check_icon() -> QIcon:
    def draw(painter: QPainter):
        painter.setPen(QPen(QColor(COPIED_COLOR), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        path = QPainterPath()
        path.moveTo(3, 8.5)
        path.lineTo(6.5, 12)
        path.lineTo(13, 4.5)
        painter.drawPath(path)

    return _make_icon(draw)


# ----------------------------------------------------------------------
# プリセットの登録欄 (設定の窓に入れる)
# ----------------------------------------------------------------------
def move_button(text: str, tip: str) -> QPushButton:
    """並び替えのボタン (押し続けると繰り返す)。生成の設定と同じ作り。"""
    button = QPushButton(text)
    button.setToolTip(tip)
    button.setAutoRepeat(True)
    button.setAutoRepeatDelay(400)
    button.setAutoRepeatInterval(120)
    return button


class PresetEditor(QWidget):
    """プリセットの番号を選ぶ欄、名前・temperature・指示文・送る文、「プリセットを保存」「削除」「▲」「▼」。

    並び替えたときは、すべてのアプリの番号 (前回選んだもの) を store.swap_indexes で付け替え、
    swapped(a, b) を呼ぶ (画面で持っている番号を付け替えるため。なくてもよい)。
    """

    def __init__(self, caption: str, presets: list[LlmPreset], file_name: str, default_template: str,
                 system_caption: str, template_caption: str, template_tip: str,
                 temperature_tip: str, swapped: Optional[Callable[[int, int], None]], index: int, parent=None):
        super().__init__(parent)
        self.presets = presets
        self.file_name = file_name
        self.default_template = default_template
        self._swapped = swapped
        self.shown = -1
        self._relabeling = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(caption))
        self.box = QComboBox()
        self.box.setMaxVisibleItems(20)
        for i, preset in enumerate(presets):
            self.box.addItem(preset.label(i))
        layout.addWidget(self.box)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("名前", self.name_edit)
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setToolTip(temperature_tip)
        form.addRow("temperature", self.temperature_spin)
        layout.addLayout(form)

        layout.addWidget(QLabel(system_caption))
        self.system_edit = QPlainTextEdit()
        layout.addWidget(self.system_edit, 3)
        layout.addWidget(QLabel(template_caption))
        self.template_edit = QPlainTextEdit()
        self.template_edit.setToolTip(template_tip)
        layout.addWidget(self.template_edit, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        up = move_button("▲ 上へ", "選んでいるプリセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        down = move_button("▼ 下へ", "選んでいるプリセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addSpacing(24)
        save_button = QPushButton("プリセットを保存")
        delete_button = QPushButton("削除")
        save_button.clicked.connect(self.store)
        delete_button.clicked.connect(self.delete)
        buttons.addWidget(save_button)
        buttons.addWidget(delete_button)
        layout.addLayout(buttons)

        self.box.currentIndexChanged.connect(self._on_select)
        self.select(min(max(index, 0), PRESET_COUNT - 1))

    def select(self, index: int):
        self.box.setCurrentIndex(index)
        self._on_select(index)

    def _on_select(self, index: int):
        """番号を選ぶと、その中身を欄に出す (保存していない入力は捨てる)。"""
        if self._relabeling or index < 0:
            return
        self.shown = index
        self._show(self.presets[index])

    def _show(self, preset: LlmPreset):
        self.name_edit.setText(preset.name)
        self.system_edit.setPlainText(preset.system)
        self.template_edit.setPlainText(preset.template)
        self.temperature_spin.setValue(preset.temperature)

    def _edited(self) -> LlmPreset:
        return LlmPreset(
            name=self.name_edit.text().strip(),
            system=self.system_edit.toPlainText().strip(),
            template=self.template_edit.toPlainText().strip() or self.default_template,
            temperature=round(self.temperature_spin.value(), 2),
        )

    def _reload(self):
        """ほかのアプリで変えたものを消さないよう、書く前にファイルを読み直す (まだなければ今の内容のまま)。"""
        if store.load(self.file_name) is not None:
            self.presets[:] = load_presets(self.file_name, [], self.default_template)

    def _relabel_all(self):
        self._relabeling = True
        for i, preset in enumerate(self.presets):
            self.box.setItemText(i, preset.label(i))
        self.box.setCurrentIndex(self.shown)
        self._relabeling = False

    def store(self):
        edited = self._edited()
        self._reload()
        self.presets[self.shown] = edited
        self._saved()

    def delete(self):
        self._reload()
        self.presets[self.shown] = LlmPreset(template=self.default_template)
        self._show(self.presets[self.shown])
        self._saved()

    def _saved(self):
        self._relabel_all()
        save_presets(self.file_name, self.presets)

    def _move(self, step: int):
        """隣の番号と入れ替えて、すぐ保存する。欄の内容 (保存していない入力も) はそのまま付いていく。"""
        a, b = self.shown, self.shown + step
        if a < 0 or b < 0 or b >= len(self.presets):
            return
        self._reload()
        self.presets[a], self.presets[b] = self.presets[b], self.presets[a]
        self.shown = b
        self._relabel_all()
        save_presets(self.file_name, self.presets)
        store.swap_indexes(self.file_name, a, b)
        if self._swapped is not None:
            self._swapped(a, b)


class ModelPicker(QWidget):
    """LLM のモデル名の欄と「モデル取得」。結果は show_status(文) で知らせる。"""

    _done = Signal(object, object)

    def __init__(self, model: str, base_url: Callable[[], str], show_status: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._show_status = show_status
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setEditText(model)
        row.addWidget(self.combo, 1)
        self.button = QPushButton("モデル取得")
        self.button.setToolTip("サーバーにあるモデルの一覧を取って、欄から選べるようにします")
        row.addWidget(self.button)
        self.button.clicked.connect(self.fetch)
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    def model(self) -> str:
        return self.combo.currentText().strip()

    def fetch(self):
        self.button.setEnabled(False)
        self._show_status("モデル一覧を取得中...")
        url = self._base_url()
        llm.run_in_thread(lambda: llm.list_models(url), self._done)

    def _on_done(self, models, error):
        self.button.setEnabled(True)
        if error:
            self._show_status("モデル取得失敗: " + error)
            return
        if not models:
            self._show_status("モデルが見つかりませんでした。")
            return
        current = self.model()
        picked = llm.pick_model(current, models)
        self.combo.clear()
        self.combo.addItems(models)
        self.combo.setEditText(picked)
        message = f"モデル {len(models)} 件を取得しました。"
        if picked != current:
            message += f"「{current}」はサーバーにないので「{picked}」にしました。"
        self._show_status(message)


def fill_combo(combo: QComboBox, presets: list[LlmPreset], index: Optional[int]):
    combo.blockSignals(True)
    combo.clear()
    for i, preset in enumerate(presets):
        combo.addItem(preset.label(i))
    combo.setCurrentIndex(min(max(index or 0, 0), len(presets) - 1))
    combo.blockSignals(False)
