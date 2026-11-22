"""「PrismLab のレシピ」の段: 台を選び、ファイルを選んでレシピのボタンを押すと生成が始まる (選んでポン)。

PromptWordCrafter・MuseFactory で共通 (recipe_link.py。PrismLab の docs\\spec.md 6 章)。
段の中身: [台: A · B] [レシピ 1] [レシピ 2] … [PrismLab で直す...] / 進み具合の行 [中止]
"""

import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPolygonF
from PySide6.QtWidgets import (QApplication, QDialog, QGroupBox, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from . import app, machines, recipe_link

POLL_MS = 1500  # 進み具合・出してもらったレシピを読み直す間隔
CONFIRM_COUNT = 30  # これを超える件数を頼むときは確かめる
SHOW_DONE_SECONDS = 10 * 60  # 終わったものも、これだけのあいだは進み具合の行に出す


HAZARD_YELLOW = QColor("#f5c400")
HAZARD_BLACK = QColor("#1a1a1a")


class HazardButton(QPushButton):
    """レシピのボタン (選んでポンの「ポン」)。黄色と黒のななめ縞の縁取り (工事現場ふう。2026-11-22 利用者の希望)。
    押せないとき (入口に合わないレシピ) は縞を灰色にする。"""

    BORDER = 4  # 縞の縁の太さ
    STRIPE = 7  # 縞 1 本の幅
    RADIUS = 5

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self.text()) + 26 + 2 * self.BORDER, fm.height() + 12 + 2 * self.BORDER)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        on = self.isEnabled()
        yellow = HAZARD_YELLOW if on else QColor("#a8a8a8")
        black = HAZARD_BLACK if on else QColor("#6a6a6a")
        outer = QPainterPath()
        outer.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        p.setClipPath(outer)
        p.fillRect(rect, black)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(yellow)
        h, w = rect.height(), self.STRIPE
        x = -h
        while x < rect.width() + h:  # 右上がりのななめ縞
            p.drawPolygon(QPolygonF([QPointF(x, h), QPointF(x + w, h), QPointF(x + w + h, 0), QPointF(x + h, 0)]))
            x += 2 * w
        p.setClipping(False)
        inner = rect.adjusted(self.BORDER, self.BORDER, -self.BORDER, -self.BORDER)
        back = self.palette().button().color()
        if on and self.isDown():
            back = back.darker(118)
        elif on and self.underMouse():
            back = back.lighter(112) if back.lightness() < 128 else back.darker(106)
        # 内側は無地 (Windows 11 のボタンの色は半透明で縞が透けるので、先に窓の背景色で塗りつぶす)
        solid = QColor(self.palette().window().color())
        solid.setAlpha(255)
        p.setBrush(solid)
        p.drawRoundedRect(inner, self.RADIUS - 2, self.RADIUS - 2)
        p.setBrush(back)
        p.drawRoundedRect(inner, self.RADIUS - 2, self.RADIUS - 2)
        p.setPen(self.palette().buttonText().color() if on else self.palette().color(
            self.palette().ColorGroup.Disabled, self.palette().ColorRole.ButtonText))
        font = self.font()
        font.setBold(on)
        p.setFont(font)
        p.drawText(inner, Qt.AlignmentFlag.AlignCenter, self.text())


class RecipePanel(QGroupBox):
    """files(): 今選んでいるファイル (なければ空)。outputs_added: 頼んだものに新しく出来たファイルがある (生成結果の帯を読み直す)。"""

    outputs_added = Signal()
    status = Signal(str)  # ステータスバーに出す文

    def __init__(self, files: Callable[[], list[Path]], parent=None):
        super().__init__("生成 (PrismLab のレシピ)", parent)
        self.files = files
        self._stamp = -1.0
        self._recipes: list[recipe_link.PublishedRecipe] = []
        self._mine: dict[str, float] = {}  # このアプリを開いてから頼んだもの (番号 → 頼んだ時刻)
        self._outputs: dict[str, int] = {}
        self._last: dict[str, str] = {}  # 番号 → 前に見た状態 (終わったときに知らせる)
        hint = app.profile().colors.hint

        box = QVBoxLayout(self)
        box.setContentsMargins(6, 4, 6, 4)
        row = QHBoxLayout()
        self.machines_button = QPushButton()
        self.machines_button.setToolTip("このアプリで使う ComfyUI の台を 3 台まで選びます (休みの切り替えも)。"
                                        "台の空きは PrismLab が 1 か所で数えます (1 台に同時に 2 件まで)。")
        self.machines_button.clicked.connect(self._edit_machines)
        row.addWidget(self.machines_button)
        self.buttons_host = QWidget()
        self.buttons_row = QHBoxLayout(self.buttons_host)
        self.buttons_row.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidget(self.buttons_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(HazardButton("A").sizeHint().height() + 8)
        row.addWidget(scroll, 1)
        self.edit_button = QPushButton("PrismLab で直す...")
        self.edit_button.setToolTip("レシピの中身・出すレシピは PrismLab で決めます (量産ラインの「ほかのアプリに出す」)")
        self.edit_button.clicked.connect(self._open_prismlab)
        row.addWidget(self.edit_button)
        box.addLayout(row)

        srow = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {hint};")
        self.cancel_button = QPushButton("中止...")
        self.cancel_button.setToolTip("頼んだ生成を止めます (出来たものは残します)")
        self.cancel_button.clicked.connect(self._cancel_menu)
        srow.addWidget(self.status_label, 1)
        srow.addWidget(self.cancel_button)
        box.addLayout(srow)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(POLL_MS)
        self._show_machines()
        self.poll()

    # ------------------------------------------------------------------
    def _show_machines(self) -> None:
        on = [machines.name_of(u) for u in machines.machines()]
        self.machines_button.setText("台: " + (" · ".join(on) if on else "なし"))

    def _edit_machines(self) -> None:
        if machines.MachinesDialog(self).exec() == QDialog.DialogCode.Accepted:
            self._show_machines()

    def _open_prismlab(self) -> None:
        error = recipe_link.open_prismlab()
        if error:
            QMessageBox.warning(self, "PrismLab", error)

    def _rebuild(self) -> None:
        while self.buttons_row.count():
            item = self.buttons_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not self._recipes:
            note = QLabel("PrismLab で「ほかのアプリに出す」にしたレシピが、ここに並びます。")
            note.setStyleSheet(f"color: {app.profile().colors.hint};")
            self.buttons_row.addWidget(note)
        for r in self._recipes:
            button = HazardButton(r.name or "(名前なし)")
            tip = r.flow + (f"\n回数 {r.repeat}" if r.repeat > 1 else "")
            tip += f"\n\n⚠ {r.problem}" if r.problem else "\n\n選んでいるファイルを、このレシピで生成します (PrismLab が裏で動かします)。"
            button.setToolTip(tip)
            button.setEnabled(not r.problem)
            button.clicked.connect(lambda _c=False, rr=r: self.start(rr))
            self.buttons_row.addWidget(button)
        self.buttons_row.addStretch(1)

    def start(self, recipe: recipe_link.PublishedRecipe, files=None) -> None:
        """レシピで生成する (ボタン・右クリックのメニューから)。files はファイルの並びか、それを返す関数
        (フォルダのサブフォルダまで数えるときなど、選んだときに数える)。None なら今選んでいるもの。"""
        if callable(files):
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                files = files()
            finally:
                QApplication.restoreOverrideCursor()
        files = list(files if files is not None else self.files())
        if not files:
            self.status.emit("生成するテキストファイルを選んでください。")
            return
        total = len(files) * max(1, recipe.repeat)
        if total > CONFIRM_COUNT and QMessageBox.question(
                self, "PrismLab のレシピ", f"「{recipe.name}」で {total} 件を生成します。よろしいですか？",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
            return
        rid, error = recipe_link.run(recipe, files)
        if error:
            QMessageBox.warning(self, "PrismLab のレシピ", error)
            return
        self._mine[rid] = time.time()
        what = files[0].name if len(files) == 1 else f"{len(files)} 件"
        self.status.emit(f"「{recipe.name}」で {what} の生成を PrismLab に頼みました (裏で動きます)。")
        self.poll()

    def recipes(self) -> list[recipe_link.PublishedRecipe]:
        return list(self._recipes)

    def recipe_menu(self, menu: QMenu, files, title: str = "PrismLab のレシピで生成") -> QMenu:
        """右クリックのメニューに「PrismLab のレシピで生成 ▸」を足す。files はファイルの並びか、それを返す関数。"""
        sub = QMenu(title, menu)  # 親を付けて作る (addMenu("名前") のものは関数を抜けると消える)
        menu.addMenu(sub)
        self._fill_menu(sub, files)
        return sub

    def _fill_menu(self, sub: QMenu, files) -> None:
        if not self._recipes:
            sub.addAction("(出してもらったレシピがありません。PrismLab の「ほかのアプリに出す」)").setEnabled(False)
        for r in self._recipes:
            action = sub.addAction(r.name or "(名前なし)")
            action.setToolTip(r.problem or r.flow)
            action.setEnabled(not r.problem and (callable(files) or bool(files)))
            action.triggered.connect(lambda _c=False, rr=r: self.start(rr, files))
        sub.setToolTipsVisible(True)

    def choose_and_start(self, files: list[Path], parent=None) -> None:
        """レシピを選ぶメニューをマウスの位置に出し、選んだレシピで生成する (作業タブの「保存して生成する...」)。"""
        if not files:
            return
        self.poll()
        menu = QMenu(parent or self)
        title = menu.addAction(f"{files[0].name if len(files) == 1 else f'{len(files)} 件'} を、どのレシピで生成しますか")
        title.setEnabled(False)
        menu.addSeparator()
        self._fill_menu(menu, files)
        menu.exec(QCursor.pos())

    # ------------------------------------------------------------------
    def poll(self) -> None:
        stamp = recipe_link.published_stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            self._recipes = recipe_link.recipes()
            self._rebuild()
        now = time.time()
        sts = recipe_link.statuses()
        shown = [s for s in sts.values() if s.active or s.id in self._mine
                 and now - float(s.finished_at or s.created or now) < SHOW_DONE_SECONDS]
        shown.sort(key=lambda s: s.created)
        added = False
        for s in sts.values():
            if s.outputs > self._outputs.get(s.id, 0) and s.id in self._outputs:
                added = True
            self._outputs[s.id] = s.outputs
            before = self._last.get(s.id)
            if before == "running" and s.state != "running" and s.id in self._mine:
                self.status.emit(_line(s))
            self._last[s.id] = s.state
        waiting = [rid for rid in self._mine if rid not in sts and now - self._mine[rid] < 60]
        lines = [_line(s) for s in shown]
        if waiting:
            lines.append(f"PrismLab の受け付けを待っています ({len(waiting)} 件)...")
        down = {m.get("name"): m.get("reason") for s in shown if s.active for m in s.machines
                if m.get("state") != "up" and m.get("reason")}
        lines += [f"台 {name}: {reason}" for name, reason in down.items()]
        self.status_label.setText("\n".join(lines))
        self.status_label.setVisible(bool(lines))
        self._active = [s for s in shown if s.active]
        self.cancel_button.setVisible(bool(self._active))
        if added:
            self.outputs_added.emit()

    def _cancel_menu(self) -> None:
        active = getattr(self, "_active", [])
        if not active:
            return
        menu = QMenu(self)
        for s in active:
            menu.addAction(f"「{s.recipe}」を止める ({s.done + s.failed}/{s.total})",
                           lambda rid=s.id, name=s.recipe: self._cancel(rid, name))
        if len(active) > 1:
            menu.addSeparator()
            menu.addAction("すべて止める", lambda: [self._cancel(s.id, s.recipe, ask=False) for s in active])
        menu.exec(self.cancel_button.mapToGlobal(self.cancel_button.rect().bottomLeft()))

    def _cancel(self, rid: str, name: str, ask: bool = True) -> None:
        if ask and QMessageBox.question(self, "中止", f"「{name}」の生成を止めますか？ (出来たものは残します)",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        error = recipe_link.cancel(rid)
        if error:
            QMessageBox.warning(self, "中止", error)
        self.poll()


def _line(s: recipe_link.Status) -> str:
    if s.state == "error":
        return f"✗「{s.recipe}」: {s.error}"
    finished = s.done + s.failed + s.canceled
    extra = [f"失敗 {s.failed}" if s.failed else "", f"★待ち {s.gated}" if s.gated else ""]
    extra = "・".join(e for e in extra if e)
    if s.state == "running":
        detail = f"実行中 {s.running}" + (f"・待機 {s.waiting}" if s.waiting else "")
        return f"▶「{s.recipe}」{finished}/{s.total} 件 ({detail}{'・' + extra if extra else ''})"
    word = "止めました" if s.state == "canceled" else "完了"
    text = f"{'■' if s.state == 'canceled' else '✓'}「{s.recipe}」{word}: {s.done}/{s.total} 件 (出来たファイル {s.outputs})"
    if s.failed:
        text += f"  失敗 {s.failed}" + (f": {s.errors[0]}" if s.errors else "")
    return text
