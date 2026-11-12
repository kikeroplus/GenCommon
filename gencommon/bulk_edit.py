"""登録の一覧をまとめて編集する画面 (接続先・ワークフロー・セット・エンハンサー・読み取りプリセット)。

- チェックしたものを削除 (未登録) にする
- 選んだものを上へ / 下へ動かす、名前順・名前の逆順に並べる、元の順に戻す
- 「空きを詰める」: オフなら登録してある番号の位置はそのまま (中身だけ入れ替わる)、オンなら 1 番から詰めて並べる

画面は並び (元の番号の並び) と削除するものを決めるだけ。書き込みは呼び出し側が plan() の結果で行う。
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

T = TypeVar("T")

_NUMBER = re.compile(r"^\d+:\s*")


def strip_number(label: str) -> str:
    """「3: 名前」の番号を外す。"""
    return _NUMBER.sub("", label, count=1)


def natural_key(text: str):
    """名前順の比べ方 (大文字小文字を区別せず、数字は数として比べる: wf2 < wf10)。"""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in re.split(r"(\d+)", text.casefold()) if part]


@dataclass
class Result:
    order: list[int]  # 残すものの元の番号 (0 始まり) を、新しい並びの順に
    deleted: list[int] = field(default_factory=list)  # 削除するものの元の番号
    pack: bool = False  # 空きを詰めて 1 番から並べる

    def plan(self) -> dict[int, int]:
        """元の番号 → 新しい番号。空きを詰めないときは、残すものが元いた番号を小さい順に使う。"""
        slots = list(range(len(self.order))) if self.pack else sorted(self.order)
        return {old: slots[i] for i, old in enumerate(self.order)}

    @property
    def changed(self) -> bool:
        return bool(self.deleted) or any(old != new for old, new in self.plan().items())


def apply(current: list[T], listed: list[int], result: Result, empty: Callable[[], T],
          is_empty: Callable[[T], bool]) -> tuple[list[T], dict[int, int]]:
    """読み直した一覧 current に結果を当てはめる。戻り値は (新しい一覧, 実際の 元の番号 → 新しい番号)。

    listed は画面に出した元の番号。画面を開いている間にほかのアプリが空きに足したもの (listed にない) は
    その場所に残し、行き先がそれと重なったときは次の空きに入れる。
    """
    items = list(current)
    for i in listed:
        if 0 <= i < len(items):
            items[i] = empty()
    mapping: dict[int, int] = {}
    taken = {i for i, item in enumerate(items) if not is_empty(item)}
    for old, new in result.plan().items():
        if not 0 <= old < len(current):
            continue
        target = new
        while target in taken and target < len(items):
            target += 1
        if target >= len(items):  # 入る場所がない (ふつうは起きない): 元の場所に戻す
            target = next((i for i in range(len(items)) if i not in taken), -1)
            if target < 0:
                continue
        items[target] = current[old]
        taken.add(target)
        mapping[old] = target
    return items, mapping


class BulkEditDialog(QDialog):
    """items は登録してあるものの (元の番号 0 始まり, 一覧に出す文字)。exec() のあと result に結果が入る。"""

    def __init__(self, what: str, items: list[tuple[int, str]], note: str = "", parent=None):
        super().__init__(parent)
        self.what = what
        self.setWindowTitle(f"{what}をまとめて編集")
        self.resize(640, 600)
        self.result_value: Optional[Result] = None
        self._original = [index for index, _text in items]
        self._texts = {index: strip_number(text) for index, text in items}
        self._updating = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            f"削除する{what}にチェックを付けます。並び順は、行を選んで「▲ 上へ」「▼ 下へ」か、名前順のボタンで変えます "
            "(Shift / Ctrl で複数を選んで一緒に動かせます)。OK を押すまでは何も変わりません。"
            + (f"\n{note}" if note else ""))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for index in self._original:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        body.addWidget(self.list, 1)

        side = QVBoxLayout()
        self.up = self._button("▲ 上へ", "選んでいる行を 1 つ上へ (押し続けると続けて動きます)", True)
        self.down = self._button("▼ 下へ", "選んでいる行を 1 つ下へ (押し続けると続けて動きます)", True)
        self.top = self._button("先頭へ", "選んでいる行をいちばん上へ")
        self.bottom = self._button("末尾へ", "選んでいる行をいちばん下へ")
        sort_up = self._button("名前順", "名前の順 (あいうえお・ABC 順。数字は数の大きさで比べます) に並べます")
        sort_down = self._button("名前の逆順", "名前の逆の順に並べます")
        reset = self._button("元の順", "開いたときの並びに戻します (チェックはそのまま)")
        for w in (self.up, self.down, self.top, self.bottom):
            side.addWidget(w)
        side.addSpacing(16)
        for w in (sort_up, sort_down, reset):
            side.addWidget(w)
        side.addStretch(1)
        body.addLayout(side)
        layout.addLayout(body, 1)

        self.pack = QCheckBox("空きを詰めて 1 番から並べる")
        self.pack.setToolTip("オフ: 登録してある番号の位置はそのままで、中身の順だけ変えます。\n"
                             "オン: 未登録の番号をなくして、1 番から順に詰めます。")
        layout.addWidget(self.pack)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        all_on = QPushButton("すべてチェック")
        all_off = QPushButton("すべて外す")
        buttons.addWidget(all_on)
        buttons.addWidget(all_off)
        buttons.addStretch(1)
        self.ok = QPushButton("OK")
        cancel = QPushButton("キャンセル")
        cancel.setDefault(True)
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

        all_on.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        all_off.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.up.clicked.connect(lambda: self._move(-1))
        self.down.clicked.connect(lambda: self._move(1))
        self.top.clicked.connect(lambda: self._move_end(True))
        self.bottom.clicked.connect(lambda: self._move_end(False))
        sort_up.clicked.connect(lambda: self._sort(False))
        sort_down.clicked.connect(lambda: self._sort(True))
        reset.clicked.connect(self._reset)
        self.pack.toggled.connect(lambda _on: self._refresh())
        self.list.itemChanged.connect(lambda _item: self._refresh())
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._refresh()

    def _button(self, text: str, tip: str, repeat: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        if repeat:
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(400)
            button.setAutoRepeatInterval(120)
        return button

    # --- 今の状態 -------------------------------------------------------
    def _rows(self) -> list[tuple[int, bool]]:
        """(元の番号, 削除する) を今の並びで。"""
        return [(self.list.item(r).data(Qt.ItemDataRole.UserRole),
                 self.list.item(r).checkState() == Qt.CheckState.Checked) for r in range(self.list.count())]

    def _result(self) -> Result:
        rows = self._rows()
        return Result([i for i, d in rows if not d], [i for i, d in rows if d], self.pack.isChecked())

    def _refresh(self):
        """行の文字 (新しい番号・元の番号・削除) と件数を出し直す。"""
        if self._updating:
            return
        self._updating = True
        result = self._result()
        plan = result.plan()
        for r in range(self.list.count()):
            item = self.list.item(r)
            old = item.data(Qt.ItemDataRole.UserRole)
            text = self._texts[old]
            font = item.font()
            if old in plan:
                new = plan[old]
                item.setText(f"{new + 1}: {text}" + ("" if new == old else f"   (元 {old + 1})"))
                font.setStrikeOut(False)
            else:
                item.setText(f"削除: {text}   (元 {old + 1})")
                font.setStrikeOut(True)
            item.setFont(font)
        self._updating = False
        moved = sum(1 for old, new in plan.items() if old != new)
        self.count_label.setText(
            f"登録 {self.list.count()} 件 / 削除 {len(result.deleted)} 件 / 番号が変わる {moved} 件")
        self.ok.setEnabled(result.changed)
        self._update_buttons()

    def _update_buttons(self):
        rows = self._selected_rows()
        n = self.list.count()
        can_up = bool(rows) and rows != list(range(len(rows)))
        can_down = bool(rows) and rows != list(range(n - len(rows), n))
        self.up.setEnabled(can_up)
        self.top.setEnabled(can_up)
        self.down.setEnabled(can_down)
        self.bottom.setEnabled(can_down)

    def _selected_rows(self) -> list[int]:
        return sorted(self.list.row(item) for item in self.list.selectedItems())

    # --- 並べ替え -------------------------------------------------------
    def _set_order(self, order: list[int], selected: set[int]):
        """行を元の番号の並び order に並べ直す (チェックは付いたまま)。selected は選んでおく元の番号。"""
        checks = {i: d for i, d in self._rows()}
        self._updating = True
        self.list.clear()
        current = None
        for index in order:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checks.get(index) else Qt.CheckState.Unchecked)
            self.list.addItem(item)
            if index in selected:
                item.setSelected(True)
                if current is None:
                    current = item
        if current is not None:
            self.list.setCurrentItem(current, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.list.scrollToItem(current)
        self._updating = False
        self._refresh()

    def _order(self) -> list[int]:
        return [i for i, _d in self._rows()]

    def _selected_ids(self) -> set[int]:
        return {item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()}

    def _move(self, step: int):
        """選んでいる行を 1 つ動かす。端に着いた行は止まり、続く行はそこで詰まる (選んだ行どうしの順は崩さない)。"""
        rows = self._selected_rows()
        if not rows:
            return
        ids = self._selected_ids()
        order = self._order()
        placed: set[int] = set()
        for r in (rows if step < 0 else list(reversed(rows))):
            t = r + step
            if 0 <= t < len(order) and t not in placed:
                order[r], order[t] = order[t], order[r]
                placed.add(t)
            else:
                placed.add(r)
        self._set_order(order, ids)

    def _move_end(self, to_top: bool):
        ids = self._selected_ids()
        if not ids:
            return
        order = self._order()
        picked = [i for i in order if i in ids]
        rest = [i for i in order if i not in ids]
        self._set_order(picked + rest if to_top else rest + picked, ids)

    def _sort(self, reverse: bool):
        order = sorted(self._order(), key=lambda i: (natural_key(self._texts[i]), i), reverse=reverse)
        self._set_order(order, self._selected_ids())

    def _reset(self):
        self._set_order(list(self._original), self._selected_ids())

    def _check_all(self, state):
        self._updating = True
        for r in range(self.list.count()):
            self.list.item(r).setCheckState(state)
        self._updating = False
        self._refresh()

    def _accept(self):
        result = self._result()
        if not result.changed:
            return
        moved = sum(1 for old, new in result.plan().items() if old != new)
        parts = []
        if result.deleted:
            parts.append(f"{len(result.deleted)} 件を削除 (未登録に)")
        if moved:
            parts.append(f"{moved} 件の番号を変更")
        if QMessageBox.question(
            self, self.windowTitle(), f"{self.what}の{'・'.join(parts)}します。よろしいですか？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.result_value = result
        self.accept()


def ask(what: str, items: list[tuple[int, str]], note: str = "", parent=None) -> Optional[Result]:
    """編集させる。登録が 1 件もなければ知らせて None。キャンセル・変更なしなら None。"""
    if not items:
        QMessageBox.information(parent, f"{what}をまとめて編集", f"登録してある{what}はありません。")
        return None
    dialog = BulkEditDialog(what, items, note, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_value is None:
        return None
    return dialog.result_value


BUTTON_TEXT = "まとめて編集..."
BUTTON_TIP = "登録してあるものをまとめて削除したり、並べ替えたり (名前順も) します"

