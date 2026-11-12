"""生成する: 図の画面 (左に設定・真ん中に上から順の処理・右に送る前の確認)。

docs/relations-columns.html の図をそのまま画面にしたもの。いつもの画面 (GenerateDialog) と同じ欄・同じ処理を
並べ替えただけなので、設定の読み書きや「生成する」の中身は GenerateDialog のものを使う。
真ん中の段をクリックすると、その段を通ったあとの文を右に出す (生成を押す前の確認。LLM はまだ呼ばない)。
読み取りの段の「今読み取る」だけは、その場で LLM に読ませて .txt に書く。
"""

import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from . import comfy, enhancer, inserts, reader, set_cards, settings
from .comfy_dialogs import GenerateDialog, _c, _hint, _one_line, _parse_seconds


class _Bridge(QObject):
    """裏のスレッドから、画面のスレッドで関数を呼ぶ (この部品は消さない)。"""

    call = Signal(object)


_bridge: Optional[_Bridge] = None


def _on_ui(func: Callable[[], None]) -> None:
    _bridge.call.emit(func)

OPTIONAL_LINE = "#b7851f"  # 選んだときだけ通る段の枠 (明るい・暗い配色どちらでも見える)


class _StepBox(QFrame):
    """真ん中の処理の段。クリックで選ぶ。"""

    clicked = Signal(str)

    def __init__(self, key: str, title: str, optional: bool):
        super().__init__()
        self.key = key
        self.optional = optional
        self.setObjectName("step")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state = QLabel()
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.state)
        self.setToolTip("クリックすると、この段を通ったあとの内容を右に出します")

    def add_button(self, text: str, tip: str) -> QPushButton:
        """段の中の小さなボタン (押しても段は選ばない)。"""
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        button.setStyleSheet("padding: 1px 10px;")
        button.setAutoDefault(False)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        self.layout().addLayout(row)
        return button

    def mousePressEvent(self, event):
        self.clicked.emit(self.key)

    def show_state(self, text: str, skipped: bool, selected: bool):
        self.state.setText(text)
        style = "dashed" if self.optional else "solid"
        if selected:
            border = "2px solid palette(highlight)"
        else:
            border = f"1.5px {style} {OPTIONAL_LINE if self.optional else 'palette(mid)'}"
        self.setStyleSheet(f"#step {{ border: {border}; border-radius: 6px; background: palette(base); }}")
        weight = "font-weight: bold;"
        self.title.setStyleSheet(weight + (f" color: {_c().hint};" if skipped else ""))
        self.state.setStyleSheet(f"color: {_c().hint};")


class DiagramGenerateDialog(GenerateDialog):
    """GenerateDialog の欄を、設定 → 処理 → 出力 の図の並びに置き直した画面。

    settings_only: 「図で設定」(メインの画面のボタン) から開いたとき。生成はせず、選んだものを
    前回の値として保存する (次に「生成する」で開いたときに、この設定で始まる)。
    """

    def __init__(self, title: str, captions: list[tuple[str, str]],
                 prefill: Optional[comfy.GenRequest], parent=None, carry: Optional[dict] = None,
                 settings_only: bool = False, sources: Optional[list[Path]] = None,
                 save_caption: Optional[Callable[[Path, str], None]] = None):
        global _bridge
        self.settings_only = settings_only  # super().__init__ の中で _update_error が呼ばれるので先に決める
        self._reading = False  # 「今読み取る」の最中
        super().__init__(title, captions, prefill, parent, carry)
        # 「今読み取る」で使う、元画像 (captions と同じ並び) と .txt の書き方 (アプリの画面にも反映する)
        self._sources = list(sources or [])
        self._save_caption = save_caption or reader.write_caption
        if _bridge is None:
            _bridge = _Bridge()
            _bridge.call.connect(lambda func: func())
        self.read_button: Optional[QPushButton] = None
        self._read_note = ""  # 「今読み取る」の結果 (読み取りの段に出す)
        self._steps: dict[str, _StepBox] = {}
        self._pointers: dict[str, QLabel] = {}
        self._selected = "generate"
        self._build()
        for signal in (self.enhancer_box.currentIndexChanged, self.reader_box.currentIndexChanged,
                       self.reread_check.toggled, self.base_check.toggled, self.wf_box.currentIndexChanged,
                       self.conn_box.currentIndexChanged, self.seconds_edit.textChanged,
                       self.repeat_edit.textChanged, self.set_box.currentIndexChanged):
            signal.connect(self._refresh)
        for row in self.insert_rows:
            row.changed.connect(self._refresh)
        self._refresh()
        # 高さは中身が収まる分 (決めた高さで縮めると、欄が重なる)
        self.resize(1180, max(self.minimumSizeHint().height(), self.sizeHint().height()))

    @property
    def _no_file(self) -> bool:
        """「図で設定」でファイルを選ばずに開いた (名前が空)。"""
        return not self.captions[0][0]

    # --- 並べ直し ------------------------------------------------------
    def _build(self):
        pf = self.profile
        # いつもの画面の並びを外す。使う欄は新しい並びに入れ直し、使わない欄も (処理から参照するので) 隠して残す
        holder = QWidget()
        holder.setLayout(self.layout())

        root = QVBoxLayout(self)
        # セット: 中身の概要が見えるカードを横に並べる (選んでいる番号は隠した set_box が持つ)
        self._wf_kinds: dict = {}  # ワークフローが動画か画像か (カードを描き直すたびに読まないように)
        self.set_strip = set_cards.SetStrip(len(self._sets), self.set_box.setCurrentIndex)
        set_row = QHBoxLayout()
        set_label = QLabel("セット")
        set_label.setStyleSheet("font-weight: bold;")
        set_row.addWidget(set_label, 0, Qt.AlignmentFlag.AlignTop)
        set_row.addWidget(self.set_strip, 1)
        set_buttons = QVBoxLayout()
        set_buttons.addWidget(self.save_set)
        set_buttons.addWidget(self.delete_set)
        set_buttons.addStretch(1)
        set_row.addLayout(set_buttons)
        root.addLayout(set_row)
        self.set_box.currentIndexChanged.connect(lambda _n: self._refresh_sets())
        self._refresh_sets()
        root.addWidget(self.error_label)

        # 行ごとに「設定 → 処理」を並べる。真ん中の列は幅を決めて、どの行でも同じ位置にそろえる
        # (QGridLayout は折り返すラベルの高さを正しく取れず欄が重なるので、行ごとの並びにする)
        rows = QVBoxLayout()
        rows.setSpacing(4)
        rows.addLayout(self._row(self._head("ユーザーが設定する"), None, self._head("処理 (上から順に)"), None))

        steps = []
        if pf.reads:
            box = self._group("元画像と読み取り (読み取りプリセットは共有)")
            box.layout().addWidget(self.prompt_label)
            box.layout().addWidget(self.prompt_view)
            row = QHBoxLayout()
            row.addWidget(QLabel("読み取り"))
            row.addWidget(self.reader_box, 1)
            row.addWidget(self.reader_settings_button)
            box.layout().addLayout(row)
            box.layout().addWidget(self.reread_check)
            steps.append(("source", "読み取り", True, box))
        else:
            box = self._group(f"元にするもの ({pf.source_word}の{pf.prompt_word})")
            box.layout().addWidget(self.prompt_label)
            box.layout().addWidget(self.prompt_view)
            steps.append(("source", pf.prompt_word, False, box))

        box = self._group("エンハンサー (共有)")
        row = QHBoxLayout()
        row.addWidget(self.enhancer_box, 1)
        row.addWidget(self.enhancer_settings_button)
        box.layout().addLayout(row)
        box.layout().addWidget(self.enhancer_hint)
        steps.append(("enhance", "エンハンサーで盛る", True, box))

        self.insert_group.setTitle("差し込み (1 件ごとに、フォルダの次のテキストを足す)")
        steps.append(("insert", "差し込みを付ける", True, self.insert_group))

        box = self._group("ベースプロンプト (共有)")
        box.layout().addWidget(self.base_check)
        self._no_base = _hint("ベースプロンプトは未設定です (「生成の設定...」で設定します)")
        box.layout().addWidget(self._no_base)
        steps.append(("base", "ベースプロンプトを付ける", True, box))

        box = self._group("ワークフロー (共有)")
        box.layout().addWidget(self.wf_box)
        box.layout().addWidget(self.source_hint)
        box.layout().addWidget(self.machine_hint)
        row = QHBoxLayout()
        row.addWidget(QLabel("長さ"))
        row.addWidget(self.seconds_edit)
        row.addWidget(self.seconds_unit)
        row.addStretch(1)
        box.layout().addLayout(row)
        steps.append(("workflow", "ワークフローに入れる", False, box))

        box = self._group("接続先 (共有)")
        box.layout().addWidget(self.conn_box)
        if not self.settings_only:  # 回数は生成のたびに決めるもの (覚えない)
            row = QHBoxLayout()
            row.addWidget(QLabel("回数"))
            row.addWidget(self.repeat_edit)
            row.addWidget(QLabel("回ずつ" if self.batch else "回"))
            row.addStretch(1)
            box.layout().addLayout(row)
        steps.append(("generate", "ComfyUI で生成", False, box))

        for i, (key, title, optional, box) in enumerate(steps):
            arrow = QLabel("→")
            arrow.setStyleSheet(f"color: {_c().hint}; font-size: 14pt;")
            step = _StepBox(key, f"{i + 1}. {title}", optional)
            step.clicked.connect(self._select)
            self._steps[key] = step
            pointer = QLabel()
            pointer.setStyleSheet("color: palette(highlight); font-size: 14pt;")
            self._pointers[key] = pointer
            rows.addLayout(self._row(box, arrow, step, pointer), 1 if key == "source" else 0)
            if key == "source" and pf.reads and self._sources and not self._no_file:
                self.read_button = step.add_button(
                    "今読み取る", "生成を待たずに、今 LLM で読み取って .txt に書きます (読み取り済みのものは読み直します)")
                self.read_button.clicked.connect(self._read_now)
            if i < len(steps) - 1:
                down = QLabel("↓")
                down.setAlignment(Qt.AlignmentFlag.AlignCenter)
                down.setStyleSheet(f"color: {_c().hint};")
                rows.addLayout(self._row(None, None, down, None))

        out = QGroupBox("出力")
        out_layout = QVBoxLayout(out)
        self.preview_title = QLabel()
        self.preview_title.setStyleSheet("font-weight: bold;")
        self.preview_title.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        out_layout.addWidget(self.preview_title)
        out_layout.addWidget(self.preview, 1)
        made = ("説明プロンプト (.txt)・" if pf.reads else "") + "出来た画像・動画・生成の記録"
        out_layout.addWidget(_hint(f"生成すると出来るもの: {made}"))
        out_column = QVBoxLayout()
        out_column.addWidget(self._head("出力 (送る前の確認)"))
        out_column.addWidget(out, 1)
        body = QHBoxLayout()
        body.addLayout(rows, 3)
        body.addLayout(out_column, 2)
        root.addLayout(body, 1)

        self.prompt_view.setMinimumHeight(70)
        if self._no_file:
            self.prompt_label.setText(
                f"{pf.source_word}を選んでいません (選んでから開くと、その{pf.prompt_word}で送る前の確認を見られます)")
            self.prompt_view.hide()
        self.prompt_view.setMaximumHeight(16777215)
        self.view_button.setText("いつもの画面で開く")
        self.view_button.setToolTip("いつもの生成の画面に切り替えます (入力した内容はそのまま)")
        if self.settings_only:
            self.ok_button.setText("設定を保存")
            self.ok_button.setToolTip("選んだものを、次に「生成する」で開いたときの設定として保存します (生成はしません)")
            self.view_button.hide()
            self.copy_button.hide()
            self.paste_button.hide()
        bottom = QHBoxLayout()
        bottom.addWidget(self.settings_button)
        bottom.addSpacing(12)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.paste_button)
        bottom.addStretch(1)
        bottom.addWidget(self.view_button)
        bottom.addSpacing(12)
        bottom.addWidget(self.ok_button)
        bottom.addWidget(self.cancel_button)
        root.addLayout(bottom)

        # 使わなかった欄 (読み取りのないアプリの読み取り欄など) は、消えないようにこの画面の子にして隠す
        for value in list(vars(self).values()):
            if isinstance(value, QWidget) and value.parent() is holder:
                value.setParent(self)
                value.hide()
        holder.deleteLater()

    # --- セットのカード -------------------------------------------------
    def _refresh_sets(self):
        connections = list(zip(self._conn_name, self._conn_url))
        workflows = list(zip(self._wf_name, self._wf_path))
        infos = set_cards.describe_sets(self._sets, connections, workflows, self.profile.reads, self._wf_kinds,
                                        self.machine_conflict)
        self.set_strip.refresh(infos, self.set_box.currentIndex())

    def _relabel_set(self, n: int):
        """保存・削除のあと (中身が変わったので、そのカードを描き直す)。"""
        super()._relabel_set(n)
        self._wf_kinds.pop(self._sets[n].workflow.lower(), None)
        self._refresh_sets()

    STEP_WIDTH = 250  # 真ん中の列の幅
    ARROW_WIDTH = 26

    @staticmethod
    def _head(text: str) -> QLabel:
        head = QLabel(text)
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setStyleSheet("font-weight: bold; font-size: 11pt;")
        return head

    def _row(self, left, arrow, middle, pointer) -> QHBoxLayout:
        """設定 (幅は残り) → 処理 (幅を決める) ▶ の 1 行。None のところは空ける。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        if left is None:
            row.addStretch(1)
        else:
            row.addWidget(left, 1)
        for widget, width in ((arrow, self.ARROW_WIDTH), (middle, self.STEP_WIDTH), (pointer, self.ARROW_WIDTH)):
            holder = widget if widget is not None else QWidget()
            holder.setFixedWidth(width)
            row.addWidget(holder, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    @staticmethod
    def _group(title: str) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        return box

    # --- 「図で設定」: 生成せずに前回の値として保存 ------------------------
    def _update_error(self):
        super()._update_error()
        if getattr(self, "_reading", False):
            self.ok_button.setEnabled(False)  # 今読み取っている最中は生成しない (ジョブでも読み取ってしまうので)
        elif self.settings_only:
            # 本文が空でも設定は保存できる。出すのはワークフローの問題だけ
            error = self._workflow_error
            self.error_label.setText(error or "")
            self.error_label.setVisible(error is not None)
            self.ok_button.setEnabled(True)

    def _accept(self):
        if self._reading:
            return
        if not self.settings_only:
            super()._accept()
            return
        sel = max(self.conn_box.currentIndex(), 0)
        if self._conn_index[sel] >= 0:
            comfy.set_active_connection(self._conn_index[sel])
        wf = self.wf_box.currentIndex()
        if wf >= 0:
            comfy.set_active_workflow(self._wf_index[wf])
        if self.seconds_edit.isEnabled() and _parse_seconds(self.seconds_edit.text()) is not None:
            comfy.set_last_seconds(self.seconds_edit.text())
        comfy.set_last_set(self.set_box.currentIndex())
        if self.profile.reads:
            index = self._current_reader()
            if index >= 0:
                reader.set_last_generate_index(index)
            comfy.save("reader_reread.txt", "1" if self.reread_check.isChecked() else "0")
        enhancer.set_last_generate_index(self._current_enhancer())
        inserts.save_slots(self._current_slots())
        comfy.set_use_base_prompt(self.base_check.isChecked())
        self.accept()

    # --- 今読み取る ----------------------------------------------------
    def _read_targets(self) -> list[int]:
        """今読み取るもの: 未読み取り (読み直すときはすべて)。すべて読み取り済みなら、すべて読み直す。"""
        if not self._sources:
            return []
        every = list(range(len(self.captions)))
        if self.reread_check.isChecked():
            return every
        unread = [i for i, (_name, text) in enumerate(self.captions) if not text.strip()]
        return unread or every

    def _read_now(self):
        index = self._current_reader()
        targets = self._read_targets()
        if index < 0 or not targets or self._reading:
            return
        snap = reader.snapshot(self._readers[index])
        sources = [self._sources[i] for i in targets]
        self._reading = True
        self._read_note = f"読み取り中... 0 / {len(targets)}"
        self.ok_button.setEnabled(False)
        self._refresh()
        save = self._save_caption
        dialog = self

        def alive() -> bool:
            return isValid(dialog)

        def progress(n: int):
            if alive():
                dialog._read_note = f"読み取り中... {n} / {len(targets)}"
                dialog._refresh()

        def got(i: int, path: Path, text: str):
            save(path, text)  # 画面を閉じていても .txt には書く
            if alive():
                dialog.captions[i] = (dialog.captions[i][0], text)

        def finished(done: int, error: Optional[str]):
            if not alive():
                return
            dialog._reading = False
            dialog.unread = sum(1 for _name, text in dialog.captions if not text.strip())
            if done and dialog.reread_check.isChecked():
                dialog.reread_check.setChecked(False)  # 今読み取ったので、生成のときは読み直さない
            dialog._read_note = (f"{done} / {len(targets)} {dialog.profile.unit}を今読み取りました"
                                 if not error else f"読み取りに失敗しました ({done} / {len(targets)})")
            dialog._show_captions()
            dialog._update_error()
            dialog._refresh()
            if error:
                QMessageBox.warning(dialog, dialog.windowTitle(), f"読み取りに失敗しました。\n\n{error}")

        def work():
            done = 0
            error = None
            for n, (i, path) in enumerate(zip(targets, sources)):
                _on_ui(lambda n=n: progress(n))
                try:
                    text = reader.read_with(snap, path)
                except Exception as exc:  # noqa: BLE001 (LLM・ファイルのどの失敗も知らせて止める)
                    error = f"{path.name}: {exc}"
                    break
                _on_ui(lambda i=i, path=path, text=text: got(i, path, text))
                done += 1
            _on_ui(lambda: finished(done, error))

        threading.Thread(target=work, daemon=True).start()

    def _show_captions(self):
        """左の「元画像と読み取り」の説明プロンプトを、今の captions で出し直す。"""
        pf = self.profile
        captions = self.captions
        if self.batch:
            done = len(captions) - self.unread
            self.prompt_label.setText(
                f"{pf.prompt_word} ({len(captions)} {pf.unit}: 読み取り済み {done} {pf.unit} / 未読み取り {self.unread} {pf.unit}。"
                "未読み取りのものは、ジョブの中で読み取ります)")
            self.prompt_view.setPlainText("\n".join(
                f"{name} : {_one_line(text, 120) if text.strip() else '(未読み取り)'}" for name, text in captions))
        elif self.unread:
            self.prompt_label.setText(f"{pf.prompt_word} ({captions[0][0]}): まだ読み取っていません。ジョブの中で読み取ります。")
            self.prompt_view.hide()
        else:
            self.prompt_label.setText(f"{pf.prompt_word} ({captions[0][0]} の .txt。書き換えるときは{pf.prompt_where}で)")
            self.prompt_view.setPlainText(captions[0][1])
            self.prompt_view.show()

    # --- 設定から戻ったとき --------------------------------------------
    def _reader_settings(self):
        super()._reader_settings()
        self._refresh()

    def _enhancer_settings(self):
        super()._enhancer_settings()
        self._refresh()

    # --- 段の状態と、右の確認 ------------------------------------------
    def _select(self, key: str):
        self._selected = key
        self._refresh()

    def _refresh(self, *_args):
        if not self._steps:
            return  # 並べ直す前 (いつもの画面の初期化中)
        pf = self.profile
        name, text = self.captions[0]
        text = text.strip()
        reread = pf.reads and self.reread_check.isChecked()
        will_read = pf.reads and (reread or not text)
        e_index = self._current_enhancer()
        enh = self._enhancers[e_index] if e_index >= 0 else None
        use_base = self.base_check.isChecked() and bool(self._base_prompt)

        # 各段を通ったあとの文 (分からないところは〈〉で示す)
        after_source = f"〈読み取った{pf.prompt_word}〉" if will_read else text or f"〈{pf.prompt_word}〉"
        # 差し込み: 使う枠の次のファイル (1 件目のジョブの分)
        self._insert_items = []
        for row in self.insert_rows:
            slot = row.slot()
            look = row.peek() if slot.use and not row.problem else None
            if look is not None:
                self._insert_items.append((slot.label(row.index), slot.before, look,
                                           inserts.read_file(look.next_file)))
        before_texts = [body for _n, before, _l, body in self._insert_items if before]
        after_texts = [body for _n, before, _l, body in self._insert_items if not before]
        enh_input = inserts.join(after_source, *before_texts)
        self._enh_input = enh_input
        after_enhance = "〈盛ったプロンプト〉" if enh else enh_input
        after_insert = inserts.join(after_enhance, *after_texts)
        self._after_insert = after_insert
        final = inserts.join(after_insert, self._base_prompt if use_base else "")

        states = {}
        # 1. 読み取り / 本文
        if pf.reads:
            done = len(self.captions) - self.unread
            if self.batch:
                state = f"済み {done} / 未 {self.unread} {pf.unit}" + (" → すべて読み直す" if reread else "")
                skipped = not reread and self.unread == 0
            elif self._no_file:
                state, skipped = "未読み取りの画像は生成のときに読み取る", False
            elif will_read:
                state, skipped = ("読み直す" if text else "生成のときに読み取る"), False
            else:
                state, skipped = "読み取り済み (.txt を使う)", True
            if self._read_note:
                state = self._read_note + "\n" + state
            states["source"] = (state, skipped)
            if self.read_button is not None:
                targets = self._read_targets()
                self.read_button.setText("今読み取る" if self.unread or reread else "今読み直す")
                self.read_button.setEnabled(not self._reading and bool(targets) and self._current_reader() >= 0)
        else:
            states["source"] = (f"{len(self.captions)} {pf.unit}それぞれの{pf.prompt_word}" if self.batch
                                else f"〈{pf.prompt_word}〉" if self._no_file else f"{name} の{pf.prompt_word}", False)
        # 2. 盛る
        states["enhance"] = (enh.name if enh else "なし (素通り)", enh is None)
        # 差し込み
        if self._insert_items:
            states["insert"] = ("\n".join(
                f"{name}: {look.next_file.name}" + (" (盛る前)" if before and enh else "")
                for name, before, look, _body in self._insert_items), False)
        else:
            states["insert"] = ("なし (素通り)", True)
        # 3. ベース
        kind = "動画用" if self._wf_data is not None and comfy.is_video_workflow(self._wf_data) else "画像用"
        if not self._base_prompt:
            states["base"] = ("ベースプロンプトなし", True)
        else:
            states["base"] = (f"付ける ({kind})" if use_base else "付けない", not use_base)
        # 4. ワークフロー
        if self._wf_data is None:
            states["workflow"] = ("使えません (上の赤字を参照)", False)
        else:
            slots = comfy.prompt_slot_labels(self._wf_data)
            states["workflow"] = (f"{self._wf_name[self.wf_box.currentIndex()]} ・ {len(slots)} か所 ({kind})", False)
        # 5. 生成
        sel = max(self.conn_box.currentIndex(), 0)
        conn = self._conn_name[sel] or self._conn_url[sel]
        times = "" if self.settings_only else f" ・ {self.repeat_edit.text().strip() or '1'} 回" + ("ずつ" if self.batch else "")
        states["generate"] = (conn + times, False)

        self._no_base.setVisible(not self._base_prompt)
        for key, step in self._steps.items():
            state, skipped = states[key]
            step.show_state(state, skipped, key == self._selected)
            self._pointers[key].setText("▶" if key == self._selected else "")

        title, body = self._preview(self._selected, name, text, will_read, enh, after_source, final, use_base)
        if self.batch:
            title += f"  ※ 1 {pf.unit}目 ({name}) の例"
        elif self._no_file:
            title += f"  ※ {pf.source_word}を選んでいないので、{pf.prompt_word}は〈〉で示します"
        self.preview_title.setText(title)
        self.preview.setPlainText(body)
        # 説明の行が増えた (エンハンサーを選んだなど) ときは、欄が重ならないように窓を伸ばす
        self.layout().activate()
        need = max(self.minimumSizeHint().height(), self.sizeHint().height())
        if need > self.height():
            self.resize(self.width(), need)

    def _preview(self, key, name, text, will_read, enh, after_source, final, use_base) -> tuple[str, str]:
        pf = self.profile
        step = self._steps[key].title.text()
        if key == "source":
            if not pf.reads:
                return f"{step} のあと: {pf.prompt_word}", text or f"({pf.prompt_word}が空です)"
            index = self._current_reader()
            preset = self._readers[index].name if index >= 0 else "(未登録)"
            how = (f"読み取りプリセット「{preset}」・モデル {settings.reader_model()} で読み取ります。"
                   f"\n読み取った文は {name or '元画像'} と同じ名前の .txt に書き、次からはそれを使います。")
            if self._no_file:
                return (f"{step}",
                        f"{pf.prompt_word}がまだない{pf.source_word} (と「読み取り済みでも読み直す」のとき) は、"
                        f"生成のときに、{how}")
            if will_read and text:
                return f"{step} のあと: {pf.prompt_word}", f"生成のときに読み直します。{how}\n\n── 今の .txt ──\n{text}"
            if will_read:
                return f"{step} のあと: {pf.prompt_word}", f"まだ読み取っていません。生成のときに、{how}"
            return f"{step} のあと: {pf.prompt_word} (.txt の内容)", text
        if key == "enhance":
            if enh is None:
                return f"{step} のあと", f"エンハンサーは「なし」です。前の段の文をそのまま次へ渡します。\n\n{self._enh_input}"
            message = enhancer.user_message(enh.template, self._enh_input)
            return (f"{step}: 生成のときに LLM に送る内容",
                    f"エンハンサー「{enh.name}」・モデル {settings.enhance_model()} で盛ります。"
                    "ジョブの番が来るたびに盛るので、回数を増やすと毎回違う盛り方になります。\n\n"
                    f"── 指示 (system) ──\n{enh.system}\n\n── 送る文 ──\n{message}")
        if key == "insert":
            if not self._insert_items:
                return f"{step} のあと", "差し込みは使いません。前の段の文をそのまま次へ渡します。\n\n" + self._after_insert
            lines = []
            for name, before, look, _body in self._insert_items:
                when = "盛る前に入れる (エンハンサーに渡す)" if before else "盛ったあとにそのまま付ける"
                lines.append(f"・{name}: {look.describe()}  {when}")
            lines.append("ジョブ 1 件ごとに次のファイルへ進みます (最後まで使ったら最初に戻ります)。")
            return f"{step} のあと (1 件目の例)", "\n".join(lines) + "\n\n" + self._after_insert
        if key == "base":
            note = "" if use_base else (
                "(ベースプロンプトを付けません)\n\n" if self._base_prompt else "(ベースプロンプトは未設定です)\n\n")
            return f"{step} のあと: ComfyUI に送る本文", note + final
        if key == "workflow":
            if self._wf_data is None:
                return f"{step}", self._workflow_error or "ワークフローを読めません。"
            lines = ["本文を入れる場所:"] + [f"  ・{s}" for s in comfy.prompt_slot_labels(self._wf_data)]
            lines.append("  (タイトルに Negative とあるノードと、実際には使われない側には入れません)")
            if self.source_hint.text():
                lines.append("\n" + self.source_hint.text())
            lines.append("\n長さ: " + (f"{self.seconds_edit.text().strip()} 秒" if self.seconds_edit.isEnabled()
                                      else "このワークフローでは変えられません"))
            return f"{step}: 入れる場所", "\n".join(lines)
        sel = max(self.conn_box.currentIndex(), 0)
        wf = self.wf_box.currentIndex()
        lines = [
            f"接続先: {self._conn_name[sel] + '  ' if self._conn_name[sel] else ''}{self._conn_url[sel]}",
            f"ワークフロー: {self._wf_name[wf] if wf >= 0 else '(なし)'}",
        ]
        if self.seconds_edit.isEnabled():
            lines.append(f"長さ: {self.seconds_edit.text().strip()} 秒")
        if not self.settings_only:
            lines.append(f"回数: {self.repeat_edit.text().strip() or '1'} 回" + ("ずつ" if self.batch else ""))
        lines.append("\n── 送る本文 ──\n" + final)
        return f"{step}: 送るもの", "\n".join(lines)
