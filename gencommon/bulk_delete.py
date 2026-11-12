"""登録の一覧から、選んだものをまとめて未登録にする画面 (接続先・ワークフロー・セット・エンハンサー・読み取りプリセット)。

消した枠は空き (未登録) になるだけで、ほかの番号は動かさない (並び順は利用者が上へ / 下へで直す)。
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class BulkDeleteDialog(QDialog):
    """items は登録済みのものの (番号 0 始まり, 一覧に出す文字)。exec() のあと、選んだ番号が indexes に入る。"""

    def __init__(self, what: str, items: list[tuple[int, str]], note: str = "", parent=None):
        super().__init__(parent)
        self.what = what
        self.setWindowTitle(f"{what}をまとめて削除")
        self.resize(600, 520)
        self.indexes: list[int] = []
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"未登録にする{what}にチェックを付けてください。消した番号は空きになり、ほかの番号は動きません "
            "(並び順は「▲ 上へ」「▼ 下へ」で直します)。" + (f"\n{note}" if note else ""))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        for index, text in items:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        all_on = QPushButton("すべて選ぶ")
        all_off = QPushButton("すべて外す")
        buttons.addWidget(all_on)
        buttons.addWidget(all_off)
        buttons.addStretch(1)
        self.ok = QPushButton("削除")
        cancel = QPushButton("キャンセル")
        cancel.setDefault(True)
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        all_on.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        all_off.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.list.itemChanged.connect(lambda _item: self._update())
        self.ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        self._update()

    def _check_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _checked(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _update(self):
        n = len(self._checked())
        self.count_label.setText(f"{n} 件を選んでいます (登録 {self.list.count()} 件)")
        self.ok.setEnabled(n > 0)

    def _accept(self):
        indexes = self._checked()
        if QMessageBox.question(
            self, self.windowTitle(), f"{self.what} {len(indexes)} 件を未登録にします。よろしいですか？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.indexes = indexes
        self.accept()


def ask(what: str, items: list[tuple[int, str]], note: str = "", parent=None) -> Optional[list[int]]:
    """選ばせる。登録が 1 件もなければ知らせて None。キャンセルなら None。"""
    if not items:
        QMessageBox.information(parent, f"{what}をまとめて削除", f"登録してある{what}はありません。")
        return None
    dialog = BulkDeleteDialog(what, items, note, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.indexes:
        return None
    return dialog.indexes
