"""生成 (ComfyUI) の画面: 生成の設定、生成する、生成ジョブ、生成時の設定。

J-Rate の ComfySettingsDialog / ComfyPromptDialog / JobWindow / GenInfoDialog と同じ作り。
PromptWordCrafter と ImageMimicArt で共通。文言や欄の有無は app.Profile で切り替える。
"""

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import app, bulk_edit, comfy, enhancer, inserts, reader, store


def _c() -> app.Colors:
    """アプリの配色 (明るい配色・暗い配色)。"""
    return app.profile().colors


RUNNING_BACK = QColor(46, 125, 50, 50)  # ジョブ一覧で実行中の行 (OK の色を薄く。明るい・暗い配色どちらでも読める)

MAX_SECONDS = 60
MAX_REPEAT = 20


def _swapped(index: int, a: int, b: int) -> int:
    """a 番と b 番を入れ替えたあとの、index 番の行き先。"""
    return b if index == a else a if index == b else index


def _move_button(text: str, tip: str) -> QPushButton:
    """並び替えのボタン (押し続けると繰り返す)。"""
    button = QPushButton(text)
    button.setToolTip(tip)
    button.setAutoRepeat(True)
    button.setAutoRepeatDelay(400)
    button.setAutoRepeatInterval(120)
    return button


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {_c().hint};")
    return label


def _shorten(text: str, limit: int = 300) -> str:
    return text[:limit] + "..." if len(text) > limit else text


def copy_params(button: QPushButton, label: str, request: comfy.GenRequest) -> None:
    """パラメーターをクリップボードへ。押したボタンの表示をしばらく「コピーしました」にする。"""
    QApplication.clipboard().setText(comfy.params_to_text(request))
    button.setMinimumWidth(button.width())
    button.setText("コピーしました")
    QTimer.singleShot(1500, lambda: button.setText(label))


def paste_params() -> Optional[comfy.GenRequest]:
    return comfy.parse_params(QApplication.clipboard().text())


# ----------------------------------------------------------------------
# 生成の設定
# ----------------------------------------------------------------------
class _PresetTab:
    """タブ 1 枚分: プリセットの一覧、名前と値の欄、「プリセットを保存」「削除」「▲ 上へ」「▼ 下へ」。"""

    def __init__(self, presets, caption, name_caption, value_caption, value_widget, side_button,
                 label, normalize, file):
        self.list = presets
        self.value_widget = value_widget
        self._label = label
        self._normalize = normalize
        self._file = file  # 共有のファイル (comfy_presets.txt / comfy_workflows.txt)
        self._relabeling = False
        self.shown = -1

        self.panel = QWidget()
        layout = QVBoxLayout(self.panel)
        layout.addWidget(QLabel(caption))
        self.box = QComboBox()
        self.box.setMaxVisibleItems(20)
        for i, preset in enumerate(presets):
            self.box.addItem(label(preset, i))
        layout.addWidget(self.box)
        layout.addSpacing(6)

        layout.addWidget(QLabel(name_caption))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)
        layout.addSpacing(6)

        layout.addWidget(QLabel(value_caption))
        row = QHBoxLayout()
        row.addWidget(value_widget, 1)
        if side_button is not None:
            row.addWidget(side_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        layout.addSpacing(6)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        up = _move_button("▲ 上へ", "選んでいるプリセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        down = _move_button("▼ 下へ", "選んでいるプリセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons.addWidget(up)
        buttons.addWidget(down)
        buttons.addSpacing(24)
        self.save_button = QPushButton("プリセットを保存")
        self.delete_button = QPushButton("削除")
        self.bulk_button = QPushButton(bulk_edit.BUTTON_TEXT)
        self.bulk_button.setToolTip(bulk_edit.BUTTON_TIP)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.bulk_button)
        layout.addLayout(buttons)
        self.extra = layout  # 説明などを足す場所

        # 番号を選ぶと、その名前と値を欄に出す (保存していない入力は捨てる)
        self.box.currentIndexChanged.connect(self._on_select)

    def _set_value_text(self, text: str):
        if isinstance(self.value_widget, QPlainTextEdit):
            self.value_widget.setPlainText(text)
        else:
            self.value_widget.setText(text)

    def value_text(self) -> str:
        if isinstance(self.value_widget, QPlainTextEdit):
            return self.value_widget.toPlainText()
        return self.value_widget.text()

    def _on_select(self, index: int):
        if self._relabeling or index < 0:
            return
        self.shown = index
        self.name_edit.setText(self.list[index].name)
        self._set_value_text(self.list[index].value)

    def select(self, index: int):
        self.box.setCurrentIndex(index)
        self._on_select(index)

    @property
    def value(self) -> str:
        return self._normalize(self.value_text())

    def store(self):
        """欄の名前と値を、選んでいる番号のプリセットとして保存する (キャンセルしても残る)。

        共有の一覧なので、ほかのアプリで足したものを消さないよう、読み直してからこの枠だけを書く。
        """
        preset = comfy.Preset(self.name_edit.text().strip(), self.value)
        self.list[:] = comfy.update_preset(self._file, self.shown, preset)
        self._relabel()

    def delete(self):
        self.list[:] = comfy.update_preset(self._file, self.shown, None)
        self.name_edit.setText("")
        self._set_value_text("")
        self._relabel()

    def bulk_edit(self, what: str, note: str = "") -> Optional[bulk_edit.Result]:
        """まとめて削除・並べ替えをする。戻り値は当てはめた結果 (やめたら None)。"""
        self.list[:] = comfy.connections() if self._file == store.CONNECTIONS_FILE else comfy.workflows()
        self._relabel()
        items = [(i, self._label(p, i)) for i, p in enumerate(self.list) if not p.is_empty or p.name]
        result = bulk_edit.ask(what, items, note, self.panel)
        if result is None:
            return None
        items_after, mapping = comfy.rearrange_presets(self._file, [i for i, _t in items], result)
        self.list[:] = items_after
        # 出していた枠が動いたら、その行き先を選ぶ (消したときは同じ番号のまま、欄を出し直す)
        self.shown = mapping.get(self.shown, self.shown)
        self._relabel()
        self._on_select(self.shown)
        return result

    def _relabel(self):
        self._relabeling = True
        for i, preset in enumerate(self.list):
            self.box.setItemText(i, self._label(preset, i))
        self.box.setCurrentIndex(self.shown)
        self._relabeling = False

    def _move(self, step: int):
        """隣の番号と入れ替えて、すぐ保存する。欄の内容 (保存していない入力も) はそのまま付いていく。

        すべてのアプリの使用中の番号も、入れ替えに合わせて直す。
        """
        a, b = self.shown, self.shown + step
        if a < 0 or b < 0 or b >= len(self.list):
            return
        self.list[:] = comfy.swap_presets(self._file, a, b)
        self.shown = b
        self._relabel()


class _Worker(QObject):
    """接続テストを裏で行う。"""

    done = Signal(object)

    def __init__(self, func):
        super().__init__()
        self._func = func

    def run(self):
        self.done.emit(self._func())


class ComfySettingsDialog(QDialog):
    # 開くときに選んでおけるタブ (図の画面の各欄の「設定...」から、その欄のタブで開く)
    TABS = ("接続先", "ワークフロー", "ベースプロンプト", "セット")

    def __init__(self, parent=None, tab: str = ""):
        super().__init__(parent)
        self.setWindowTitle("生成の設定 (ComfyUI)")
        self.resize(660, 600)
        self.imported = False  # インポートして閉じたとき (呼び出し側が、読み込んだ内容で開き直す)
        self._thread: Optional[QThread] = None

        sets = comfy.sets()  # 「セット」のタブで並び替える
        self._sets = sets
        self._folders = comfy.connection_folders()  # 接続先ごとのワークフローのフォルダ (ワークフローの台を見分ける)

        test = QPushButton("接続テスト")
        self.connection = _PresetTab(
            comfy.connections(),
            f"接続先のプリセット ({comfy.PRESET_COUNT} 件まで登録できます。ここで選んだものを使います)",
            "名前 (一覧に出す名前。空でもかまいません)",
            "ComfyUI の URL",
            QLineEdit(), test,
            lambda p, i: p.label(i), lambda s: s.strip().rstrip("/"), store.CONNECTIONS_FILE,
        )

        browse = QPushButton("参照...")
        self.workflow = _PresetTab(
            comfy.workflows(),
            f"ワークフローのプリセット ({comfy.WORKFLOW_COUNT} 件まで登録できます。ここで選んだものを使います)",
            "名前 (一覧に出す名前。空ならファイル名を出します)",
            "ワークフロー (ComfyUI のメニュー「Export (API)」で保存した JSON)",
            QLineEdit(), browse,
            self._workflow_label, lambda s: s.strip().strip('"'), store.WORKFLOWS_FILE,
        )
        # 接続先のワークフローのフォルダ (URL の欄の下)。ワークフローがどの台用かを、置いてあるフォルダで見分ける
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("例: \\\\RAVEN\\SharBox\\comfyui-api\\kiwi (空なら台を決めない)")
        folder_browse = QPushButton("参照...")
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(folder_browse)
        conn_layout = self.connection.extra
        pos = conn_layout.count() - 1  # 「プリセットを保存」などのボタンの前
        conn_layout.insertWidget(pos, QLabel("ワークフローのフォルダ (この接続先用のワークフロー JSON を置くフォルダ)"))
        conn_layout.insertLayout(pos + 1, folder_row)
        conn_layout.insertSpacing(pos + 2, 6)
        self.connection.extra.addWidget(_hint(
            "ワークフローのフォルダを決めると、そのフォルダにあるワークフローはこの接続先の台用として扱います "
            "(一覧に台の名前が付き、生成の画面でワークフローを選ぶと接続先も合わせます)。"
            "どの接続先のフォルダにもないワークフローは共通 (どの台でも使う) です。「プリセットを保存」か OK で保存します。"
        ))
        folder_browse.clicked.connect(self._browse_folder)
        self.connection.box.currentIndexChanged.connect(lambda _i: self._show_folder())

        pf = app.profile()
        self.workflow.extra.addWidget(_hint(pf.workflow_hint))
        self.workflow.extra.addWidget(_hint(
            "接続先・ワークフロー・ベースプロンプトは J-Rate・PromptWordCrafter・ImageMimicArt で共有しています "
            "(ほかのアプリで変えたものもここに出ます)。"
        ))
        register = QPushButton("フォルダから登録...")
        register.setToolTip("接続先ごとのワークフローのフォルダにある JSON のうち、まだ登録していないものをまとめて登録します")
        register_row = QHBoxLayout()
        register_row.addWidget(register)
        register_row.addStretch(1)
        self.workflow.extra.addLayout(register_row)
        register.clicked.connect(self._register_from_folders)

        # ベースプロンプト: 動画用と画像用が 1 つずつ。OK で保存する
        base_panel = QWidget()
        base_layout = QVBoxLayout(base_panel)
        intro = QLabel(f"ベースプロンプト (毎回、{pf.prompt_word}のあとに続けて ComfyUI に送ります。空なら何も足しません。3 つのアプリで共有)")
        intro.setWordWrap(True)
        base_layout.addWidget(intro)
        self.base_video = QPlainTextEdit(comfy.base_prompt_video())
        self.base_image = QPlainTextEdit(comfy.base_prompt_image())
        base_layout.addWidget(QLabel("動画用 (動画を作るワークフローのとき)"))
        base_layout.addWidget(self.base_video)
        base_layout.addWidget(QLabel("画像用 (画像を作るワークフローのとき)"))
        base_layout.addWidget(self.base_image)
        base_layout.addWidget(_hint(
            "動画用か画像用かは、ワークフローのノードから自動で見分けます (「生成する」の画面に、どちらを使うかが出ます)。"
            f"{pf.prompt_word}とベースプロンプトの間は改行で区切ります。OK を押すと保存します。"
        ))

        # セット: ここでは並び替えだけ (保存と削除は「生成する」の画面で行う)
        set_panel = QWidget()
        set_layout = QVBoxLayout(set_panel)
        set_layout.addWidget(QLabel("セットの並び替え (セットの保存と削除は「生成する」の画面で行います)"))
        self.set_list = QListWidget()
        for i, s in enumerate(sets):
            self.set_list.addItem(s.label(i))
        self.set_list.setCurrentRow(max(comfy.last_set(), 0))
        set_layout.addWidget(self.set_list)
        set_buttons = QHBoxLayout()
        set_buttons.addStretch(1)
        set_up = _move_button("▲ 上へ", "選んでいるセットを 1 つ上の番号と入れ替えます (押し続けると続けて動きます)")
        set_down = _move_button("▼ 下へ", "選んでいるセットを 1 つ下の番号と入れ替えます (押し続けると続けて動きます)")
        set_up.clicked.connect(lambda: self._move_set(-1))
        set_down.clicked.connect(lambda: self._move_set(1))
        set_bulk = QPushButton(bulk_edit.BUTTON_TEXT)
        set_bulk.setToolTip(bulk_edit.BUTTON_TIP)
        set_bulk.clicked.connect(self._bulk_edit_sets)
        set_buttons.addWidget(set_bulk)
        set_buttons.addSpacing(24)
        set_buttons.addWidget(set_up)
        set_buttons.addWidget(set_down)
        set_layout.addLayout(set_buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.connection.panel, "接続先")
        self.tabs.addTab(self.workflow.panel, "ワークフロー")
        self.tabs.addTab(base_panel, "ベースプロンプト")
        self.tabs.addTab(set_panel, "セット")
        if tab in self.TABS:
            self.tabs.setCurrentIndex(self.TABS.index(tab))

        layout = QVBoxLayout(self)
        offline = store.offline_message()
        if offline:  # 別の PC と共有している置き場所に繋がらない (共有するものは保存できない)
            warning = QLabel(offline)
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {_c().error};")
            layout.addWidget(warning)
        layout.addWidget(self.tabs, 1)
        # 出力フォルダ (出力フォルダを持つアプリだけ)
        self.output_edit: Optional[QLineEdit] = None
        if pf.output_folder:
            out_row = QHBoxLayout()
            out_row.addWidget(QLabel("出力フォルダ"))
            self.output_edit = QLineEdit(comfy.output_folder())
            self.output_edit.setToolTip(pf.output_hint)
            out_row.addWidget(self.output_edit, 1)
            out_browse = QPushButton("参照...")
            out_row.addWidget(out_browse)
            layout.addLayout(out_row)
            layout.addWidget(_hint(pf.output_hint))
            out_browse.clicked.connect(self._browse_output)
        self.open_result = QCheckBox(pf.open_result_label)
        self.open_result.setChecked(comfy.open_result())
        layout.addWidget(self.open_result)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        bottom = QHBoxLayout()
        export_button = QPushButton("エクスポート...")
        import_button = QPushButton("インポート...")
        location_button = QPushButton("共有の置き場所...")
        location_button.setToolTip(
            "接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセット・LLM の接続を置くフォルダ。\n"
            "ほかの PC と同じフォルダ (ネットワークの共有フォルダ) にすると、PC の間でも共有できます"
        )
        bottom.addWidget(export_button)
        bottom.addWidget(import_button)
        bottom.addWidget(location_button)
        bottom.addStretch(1)
        ok = QPushButton("OK")
        ok.setDefault(True)
        cancel = QPushButton("キャンセル")
        bottom.addWidget(ok)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        export_button.clicked.connect(self._export)
        import_button.clicked.connect(self._import)
        location_button.clicked.connect(self._location)
        ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)

        self.connection.select(comfy.active_connection())
        self._show_folder()
        self.workflow.select(comfy.active_workflow())

        self.connection.save_button.clicked.connect(self._store_connection)
        self.workflow.save_button.clicked.connect(self._store_workflow)
        self.connection.delete_button.clicked.connect(self._delete_connection)
        self.connection.bulk_button.clicked.connect(self._bulk_edit_connections)
        self.workflow.bulk_button.clicked.connect(self._bulk_edit_workflows)
        self.workflow.delete_button.clicked.connect(lambda: self._delete(self.workflow, "ワークフロー"))
        test.clicked.connect(lambda: self._test(test))
        browse.clicked.connect(self._browse)

    def _result(self, color: str, text: str):
        self.result_label.setStyleSheet(f"color: {color};")
        self.result_label.setText(text)

    def _store(self, tab: _PresetTab, what: str):
        tab.store()
        state = "を未登録にしました" if tab.list[tab.shown].is_empty else "を保存しました"
        self._result(_c().ok, f"{what}のプリセット {tab.shown + 1} {state}")

    # --- 接続先ごとのワークフローのフォルダ ---------------------------
    def _workflow_label(self, preset, index: int) -> str:
        """ワークフローの一覧の文字 (台の名前を付ける)。"""
        text = preset.file_label(index)
        if preset.is_empty:
            return text
        machine = comfy.machine_name(preset.value, self.connection.list, self._folders)
        return text + comfy.machine_tag(preset.name or comfy.file_name(preset.value), machine)

    def _show_folder(self):
        index = self.connection.shown
        url = self.connection.list[index].value if 0 <= index < len(self.connection.list) else ""
        self.folder_edit.setText(self._folders.get(comfy._url_key(url), "") if url else "")

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "この接続先用のワークフローを置くフォルダ", self.folder_edit.text().strip().strip('"'))
        if folder:
            self.folder_edit.setText(str(Path(folder)))

    def _save_folder(self, old_url: str):
        """接続先を保存したあと、そのワークフローのフォルダも書き、ワークフローの一覧の台の名前を付け直す。"""
        url = self.connection.list[self.connection.shown].value
        comfy.set_connection_folder(url, self.folder_edit.text(), old_url)
        self._folders = comfy.connection_folders()
        self.workflow._relabel()

    def _store_connection(self):
        old_url = self.connection.list[self.connection.shown].value
        self._store(self.connection, "接続先")
        self._save_folder(old_url)

    def _delete_connection(self):
        old_url = self.connection.list[self.connection.shown].value
        self._delete(self.connection, "接続先")
        if self.connection.list[self.connection.shown].is_empty:
            self.folder_edit.clear()
            self._save_folder(old_url)

    @staticmethod
    def _edited_text(what: str, result: bulk_edit.Result, extra: str = "") -> str:
        moved = sum(1 for old, new in result.plan().items() if old != new)
        parts = []
        if result.deleted:
            parts.append(f"{len(result.deleted)} 件を未登録に")
        if moved:
            parts.append(f"{moved} 件の番号を変更")
        return f"{what}の {'し、'.join(parts)}しました" + extra

    def _bulk_edit_connections(self):
        result = self.connection.bulk_edit(
            "接続先", "接続先は 3 つのアプリで共有しています。削除した接続先のワークフローのフォルダも消します。")
        if result:
            self._folders = comfy.connection_folders()
            self._show_folder()
            self.workflow._relabel()
            self._result(_c().ok, self._edited_text("接続先", result))

    def _bulk_edit_workflows(self):
        result = self.workflow.bulk_edit(
            "ワークフロー", "ワークフローは 3 つのアプリで共有しています。削除しても JSON のファイルは消しません。")
        if result:
            extra = " (JSON のファイルはそのままです)" if result.deleted else ""
            self._result(_c().ok, self._edited_text("ワークフロー", result, extra))

    def _bulk_edit_sets(self):
        self._sets[:] = comfy.sets()  # ほかで保存したものを消さないよう読み直す
        for i, s in enumerate(self._sets):
            self.set_list.item(i).setText(s.label(i))
        items = [(i, s.label(i)) for i, s in enumerate(self._sets) if not s.is_empty]
        result = bulk_edit.ask("セット", items, "セットはこのアプリだけのものです。", self)
        if result is None:
            return
        current = comfy.sets()
        new_sets, mapping = bulk_edit.apply(
            current, [i for i, _t in items], result, comfy.ComfySet, lambda s: s.is_empty)
        self._sets[:] = new_sets
        for i, s in enumerate(self._sets):
            self.set_list.item(i).setText(s.label(i))
        comfy.set_sets(self._sets)
        last = comfy.last_set()
        if last in mapping and mapping[last] != last:
            comfy.set_last_set(mapping[last])
        row = self.set_list.currentRow()
        if row in mapping:
            self.set_list.setCurrentRow(mapping[row])
        self._result(_c().ok, self._edited_text("セット", result))

    def _register_from_folders(self):
        folders = []  # (台の名前, フォルダ)。同じフォルダは 1 回
        for p in self.connection.list:
            folder = self._folders.get(comfy._url_key(p.value), "") if not p.is_empty else ""
            if folder and all(comfy._folder_key(folder) != comfy._folder_key(f) for _n, f in folders):
                folders.append((p.name or comfy.file_name(folder.rstrip("\\/")), folder))
        if not folders:
            QMessageBox.information(
                self, self.windowTitle(),
                "接続先ごとのワークフローのフォルダが決まっていません。\n\n"
                "「接続先」のタブで接続先を選び、「ワークフローのフォルダ」を入れて保存してください。")
            return
        dialog = RegisterWorkflowsDialog(folders, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.paths:
            return
        added, rest = comfy.register_workflows(dialog.paths)
        self.workflow.list[:] = comfy.workflows()
        self.workflow._relabel()
        self.workflow.select(self.workflow.shown)
        text = f"ワークフローを {added} 件登録しました (名前はファイル名です。あとで変えられます)"
        if rest:
            text += f"。枠が足りず {rest} 件は登録できませんでした"
        self._result(_c().ok if not rest else _c().error, text)

    def _store_workflow(self):
        # JSON が空なら未登録に戻す。入っていれば確かめてから保存する
        path = self.workflow.value
        if path and not self._check(path):
            return
        self._store(self.workflow, "ワークフロー")

    def _delete(self, tab: _PresetTab, what: str):
        preset = tab.list[tab.shown]
        if not preset.is_empty or preset.name:
            text = f"{what}のプリセット {tab.shown + 1} を削除しますか？\n\n"
            if preset.name:
                text += preset.name + "\n"
            text += _shorten(preset.value)
            if QMessageBox.question(self, self.windowTitle(), text) != QMessageBox.StandardButton.Yes:
                return
        tab.delete()
        self._result(_c().ok, f"{what}のプリセット {tab.shown + 1} を削除しました")

    def _move_set(self, step: int):
        a = self.set_list.currentRow()
        b = a + step
        if a < 0 or b < 0 or b >= len(self._sets):
            return
        self._sets[a], self._sets[b] = self._sets[b], self._sets[a]
        self.set_list.item(a).setText(self._sets[a].label(a))
        self.set_list.item(b).setText(self._sets[b].label(b))
        self.set_list.setCurrentRow(b)
        comfy.set_sets(self._sets)
        last = comfy.last_set()
        if last >= 0:
            comfy.set_last_set(_swapped(last, a, b))

    def _test(self, button: QPushButton):
        url = self.connection.value
        self._result(_c().hint, "接続中...")
        button.setEnabled(False)

        def finished(error):
            button.setEnabled(True)
            if error is None:
                self._result(_c().ok, f"ComfyUI に接続できました: {url}")
            else:
                self._result(_c().error, f"接続できません: {error}")
            self._thread.quit()

        self._thread = QThread(self)
        worker = _Worker(lambda: comfy.test_connection(url))
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.done.connect(finished)
        self._thread.finished.connect(worker.deleteLater)
        self._worker = worker
        self._thread.start()

    def _browse(self):
        start = ""
        current = self.workflow.value
        if current and Path(current).is_file():
            start = str(Path(current).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "API 形式のワークフロー JSON を選択", start,
            "ワークフロー JSON (*.json);;すべてのファイル (*.*)",
        )
        if path:
            self.workflow.value_widget.setText(str(Path(path)))
            self._check(str(Path(path)))

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "生成した画像を保存するフォルダ (出力フォルダ) を選択", self.output_edit.text().strip()
        )
        if path:
            self.output_edit.setText(str(Path(path)))

    def _check(self, path: str) -> bool:
        try:
            wf = comfy.load_workflow(path)
            problem = comfy.prompt_slot_problem(wf)
            if problem:
                raise comfy.ComfyError(problem)
        except comfy.ComfyError as exc:
            self._result(_c().error, str(exc))
            return False
        self._result(_c().ok, comfy.describe_workflow(wf))
        return True

    def _export(self):
        from datetime import datetime

        path, _ = QFileDialog.getSaveFileName(
            self, "生成の設定をエクスポート",
            f"{app.profile().name}-生成設定-{datetime.now():%Y%m%d}.json",
            "生成設定 (*.json)",
        )
        if not path:
            return
        try:
            self._result(_c().ok, comfy.export_settings(path))
        except OSError as exc:
            self._result(_c().error, f"エクスポートできません: {exc}")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "生成の設定をインポート", "", "生成設定 (*.json);;すべてのファイル (*.*)"
        )
        if not path:
            return
        text = (
            "今の生成の設定 (接続先・ワークフロー・ベースプロンプト・セットなど) を、"
            f"このファイルの内容にすべて置き換えます。よろしいですか？\n\n{path}\n\n"
            "接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセットは "
            "J-Rate・PromptWordCrafter・ImageMimicArt で共有しているので、ほかのアプリの設定も変わります。\n\n"
            f"今の設定は次のファイルに控えを残します。\n{comfy.import_backup_path()}"
        )
        if QMessageBox.warning(
            self, self.windowTitle(), text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Ok:
            return
        try:
            done = comfy.import_settings(path)
        except (comfy.ComfyError, OSError) as exc:
            self._result(_c().error, f"インポートできません: {exc}")
            return
        QMessageBox.information(self, self.windowTitle(), done)
        self.imported = True
        self.reject()

    def _location(self):
        dialog = SharedLocationDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.changed:
            return
        # 共有の中身が変わったので、呼び出し側に開き直してもらう (インポートと同じ)
        self.imported = True
        self.reject()

    def _accept(self):
        """出している内容を保存して、選んでいる接続先とワークフローを使うものにする。"""
        if not self.connection.value:
            self.tabs.setCurrentIndex(0)
            QMessageBox.warning(self, self.windowTitle(), "選んでいる接続先の URL を入力してください。")
            self.connection.value_widget.setFocus()
            return
        path = self.workflow.value
        if not path:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, self.windowTitle(), "ワークフローの JSON を指定してください。")
            return
        if not self._check(path):
            self.tabs.setCurrentIndex(1)
            return
        if self.output_edit is not None:
            output = self.output_edit.text().strip().strip('"')
            if not output:
                QMessageBox.warning(self, self.windowTitle(), "生成した画像を保存する出力フォルダを指定してください。")
                self.output_edit.setFocus()
                return
            comfy.set_output_folder(output)
        old_url = self.connection.list[self.connection.shown].value
        self.connection.store()
        self._save_folder(old_url)
        self.workflow.store()
        comfy.set_base_prompt_video(self.base_video.toPlainText())
        comfy.set_base_prompt_image(self.base_image.toPlainText())
        comfy.set_active_connection(self.connection.shown)
        comfy.set_active_workflow(self.workflow.shown)
        comfy.set_open_result(self.open_result.isChecked())
        self.accept()


class RegisterWorkflowsDialog(QDialog):
    """接続先ごとのワークフローのフォルダにある、まだ登録していない JSON を選んで登録する。"""

    def __init__(self, folders: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("フォルダからワークフローを登録")
        self.resize(640, 560)
        self.paths: list[str] = []
        layout = QVBoxLayout(self)
        free = sum(1 for p in comfy.workflows() if p.is_empty and not p.name)
        intro = QLabel(
            "登録するワークフローにチェックを付けてください。名前はファイル名 (拡張子なし) になります "
            f"(あとで「ワークフロー」のタブで変えられます)。空いている枠: {free} 件")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        bold = self.list.font()
        bold.setBold(True)
        found = 0
        for machine, folder in folders:
            head = QListWidgetItem(f"{machine}   {folder}")
            head.setFont(bold)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(head)
            paths = comfy.unregistered_workflows(folder)
            if not paths:
                none = QListWidgetItem("    (まだ登録していないものはありません。フォルダが見つからないときもこう出ます)")
                none.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(none)
            for path in paths:
                item = QListWidgetItem(f"    {path.name}")
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(str(path))
                try:
                    comfy.load_workflow(str(path))
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    found += 1
                except comfy.ComfyError as exc:
                    item.setText(f"    {path.name}   (使えません: {_one_line(str(exc), 60)})")
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(item)
        buttons = QHBoxLayout()
        all_on = QPushButton("すべて選ぶ")
        all_off = QPushButton("すべて外す")
        buttons.addWidget(all_on)
        buttons.addWidget(all_off)
        buttons.addStretch(1)
        self.ok = QPushButton("登録")
        self.ok.setDefault(True)
        cancel = QPushButton("キャンセル")
        buttons.addWidget(self.ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        all_on.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        all_off.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        all_on.setEnabled(found > 0)
        all_off.setEnabled(found > 0)

    def _items(self):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                yield item

    def _check_all(self, state):
        for item in self._items():
            item.setCheckState(state)

    def _accept(self):
        self.paths = [item.data(Qt.ItemDataRole.UserRole) for item in self._items()
                      if item.checkState() == Qt.CheckState.Checked]
        self.accept()


class SharedLocationDialog(QDialog):
    """共有の置き場所 (この PC の設定)。別の PC と同じフォルダにすると、PC の間でも共有できる。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("共有の置き場所")
        self.resize(620, 0)
        self.changed = False
        current = store.location()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "接続先・ワークフロー・ベースプロンプト・エンハンサー・読み取りプリセット・LLM の接続を置くフォルダです。"
            "J-Rate・PromptWordCrafter・ImageMimicArt はここを読み書きします。\n"
            "ほかの PC でも同じフォルダ (ネットワークの共有フォルダ) にすると、PC の間でも同じ設定になります。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.edit = QLineEdit(str(current) if current else "")
        self.edit.setPlaceholderText(f"空ならこの PC だけ ({store.local_dir()})")
        row.addWidget(self.edit, 1)
        browse = QPushButton("参照...")
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addWidget(_hint(
            "新しいフォルダにすると、この PC の今の設定のうち、そのフォルダにないものだけを足します "
            "(フォルダにある接続先・ワークフローなどは消しません。ベースプロンプトと LLM の接続はフォルダにあるほうを使います)。"
            "使用中・前回選んだもの、セット、生成の記録はアプリごと・PC ごとのままです。"
            f"この PC の {store.local_dir()} には最後に読めた内容の写しを置き、フォルダに繋がらないときはそれを表示します "
            "(繋がらない間は変更を保存しません)。"
        ))
        offline = store.offline_message()
        if offline:
            warning = QLabel(offline)
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {_c().error};")
            layout.addWidget(warning)

        bottom = QHBoxLayout()
        local = QPushButton("この PC だけにする")
        local.setToolTip("フォルダの欄を空にします (OK で、今の内容をこの PC に写して、以後はこの PC だけで使います)")
        bottom.addWidget(local)
        bottom.addStretch(1)
        ok = QPushButton("OK")
        ok.setDefault(True)
        cancel = QPushButton("キャンセル")
        bottom.addWidget(ok)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        browse.clicked.connect(self._browse)
        local.clicked.connect(self.edit.clear)
        ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "共有の置き場所", self.edit.text().strip().strip('"'))
        if folder:
            self.edit.setText(str(Path(folder)))

    def _accept(self):
        folder = self.edit.text().strip().strip('"')
        current = store.location()
        if (not folder and current is None) or (folder and current is not None and store._same(Path(folder), current)):
            self.accept()
            return
        if folder:
            text = (f"共有の置き場所を次のフォルダにします。\n\n{folder}\n\n"
                    "この PC の今の設定のうち、そのフォルダにないものを足します。"
                    "この PC のほかのアプリ (J-Rate など) も、次に設定を読んだときからこのフォルダを使います。")
        else:
            text = (f"共有の置き場所をこの PC ({store.local_dir()}) に戻します。\n\n"
                    "今の内容をこの PC に写し、以後はほかの PC と共有しません。")
        if QMessageBox.question(self, self.windowTitle(), text) != QMessageBox.StandardButton.Yes:
            return
        try:
            notes = store.set_location(folder)
        except OSError as exc:
            QMessageBox.warning(self, self.windowTitle(), f"このフォルダは使えません。\n\n{exc}")
            return
        self.changed = True
        if notes:
            QMessageBox.information(self, self.windowTitle(), "\n".join(notes))
        self.accept()


# ----------------------------------------------------------------------
# 生成する
# ----------------------------------------------------------------------
class InsertRow(QWidget):
    """差し込みの 1 枠: 使う / 名前 / フォルダ / 選択... と、盛る前に入れる / 次のファイル / 最初に戻す。"""

    changed = Signal()

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        self.use = QCheckBox()
        self.use.setToolTip("オンにすると、ジョブ 1 件ごとにこのフォルダのテキストを 1 つずつ順に足します")
        top.addWidget(self.use)
        self.name = QLineEdit()
        self.name.setPlaceholderText(inserts.default_name(index))
        self.name.setFixedWidth(90)
        self.name.setToolTip("枠の名前 (記録・確認の画面に出します)")
        top.addWidget(self.name)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("テキスト (.txt) を入れたフォルダ")
        top.addWidget(self.folder, 1)
        browse = QPushButton("選択...")
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse)
        top.addWidget(browse)
        layout.addLayout(top)
        bottom = QHBoxLayout()
        bottom.addSpacing(24)
        self.before = QCheckBox("盛る前に入れる")
        self.before.setToolTip(
            "オン: プロンプトのあとに入れてから、エンハンサーで一緒に盛ります (言い換えられることがあります)。\n"
            "オフ: 盛ったあとに、そのまま付けます (一字一句そのまま送ります)。\n"
            "エンハンサーが「なし」のときは、盛る前に入れるものを先に並べます。"
        )
        bottom.addWidget(self.before)
        self.status = QLabel()
        bottom.addWidget(self.status, 1)
        self.reset_button = QPushButton("最初に戻す")
        self.reset_button.setAutoDefault(False)
        self.reset_button.setToolTip("このフォルダを、次は名前順の最初のファイルから使います (すぐに覚えます)")
        self.reset_button.clicked.connect(self._reset)
        bottom.addWidget(self.reset_button)
        layout.addLayout(bottom)
        self.problem: Optional[str] = None
        self.use.toggled.connect(self.refresh)
        self.before.toggled.connect(lambda _on: self.changed.emit())
        self.folder.editingFinished.connect(self.refresh)
        self.name.textChanged.connect(lambda _t: self.changed.emit())

    def set_slot(self, slot: inserts.Slot):
        for w in (self.use, self.before, self.name, self.folder):
            w.blockSignals(True)
        self.use.setChecked(slot.use)
        self.before.setChecked(slot.before)
        self.name.setText(slot.name)
        self.folder.setText(slot.folder)
        for w in (self.use, self.before, self.name, self.folder):
            w.blockSignals(False)
        self.refresh()

    def slot(self) -> inserts.Slot:
        return inserts.Slot(self.name.text().strip(), self.folder.text().strip(), self.use.isChecked(),
                            self.before.isChecked())

    def peek(self) -> Optional[inserts.Peek]:
        try:
            return inserts.peek(self.folder.text())
        except inserts.InsertError:
            return None

    def refresh(self, *_args):
        """次に使うファイルを出す。使う枠でフォルダを使えなければ、その理由 (problem)。"""
        self.problem = None
        folder = self.folder.text().strip()
        text, color = "", _c().hint
        if folder:
            try:
                text = inserts.peek(folder).describe()
            except inserts.InsertError as exc:
                text = str(exc)
                if self.use.isChecked():
                    self.problem = text
                    color = _c().error
        elif self.use.isChecked():
            self.problem = text = "フォルダを選んでください"
            color = _c().error
        if not self.use.isChecked() and text:
            text = "(使わない) " + text
        self.status.setText(_shorten(text, 70))
        self.status.setToolTip(text)
        self.status.setStyleSheet(f"color: {color};")
        self.reset_button.setEnabled(bool(folder) and bool(inserts.last_used(folder)))
        self.changed.emit()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "差し込みのフォルダを選択", self.folder.text().strip())
        if folder:
            self.folder.setText(str(Path(folder)))
            self.use.setChecked(True)
            self.refresh()

    def _reset(self):
        inserts.reset(self.folder.text())
        # 同じフォルダを使っているほかの枠も出し直す
        for row in self.parent().findChildren(InsertRow) if self.parent() else [self]:
            row.refresh()
        self.refresh()


class GenerateDialog(QDialog):
    """「生成する」のたびに出す、接続先・ワークフロー・読み取り・エンハンサー・長さ・回数の入力画面。

    プロンプトはファイルから取る (テキストの本文・元画像の説明プロンプト)。この画面では見せるだけで、
    書き換えない (直すのは編集欄・右の枠で)。
    captions: 対象の (ファイル名, プロンプト) の並び。読み取りのあるアプリでは、空のものはジョブの中で読み取る。
    prefill: 欄の初期値 (「生成時の設定」から開いたときや、「生成の設定...」から戻ったとき)。
    プロンプトは持たない。接続先・ワークフロー・長さ・読み取りプリセット・エンハンサーだけを使う。
    """

    def __init__(self, title: str, captions: list[tuple[str, str]],
                 prefill: Optional[comfy.GenRequest], parent=None, carry: Optional[dict] = None):
        super().__init__(parent)
        carry = carry or {}
        self.setWindowTitle(title)
        self.resize(640, 0)
        pf = app.profile()
        self.profile = pf
        self.captions = captions
        self.batch = len(captions) > 1
        self.unread = sum(1 for _name, text in captions if not text.strip()) if pf.reads else 0
        self.request: Optional[comfy.GenRequest] = None
        self.repeat = 1
        self.reader_preset: Optional[reader.LlmPreset] = None  # 読み取るときのプリセット
        self.reread = False  # 読み取り済みのものも読み直す
        self.enhancer_preset: Optional[enhancer.LlmPreset] = None  # AI で盛ってから送るときのエンハンサー
        self.reopen: Optional[comfy.GenRequest] = None  # 「生成の設定...」を押して閉じたときの各欄の内容
        self.settings_tab = ""  # 「生成の設定」をどのタブで開くか (図の画面の各欄の「設定...」から)
        self.switch_view = False  # 「図で開く」/「いつもの画面で開く」を押して閉じた
        self.carry: dict = {}  # 画面を切り替えるときに持っていく、reopen にない欄 (回数・セット・読み直す)
        self._wf_data: Optional[dict] = None  # 選んでいるワークフロー (読めないときは None)
        self._base_prompt = ""
        self._integer_seconds = False
        self._relabeling_set = False
        self._workflow_error: Optional[str] = None

        layout = QVBoxLayout(self)

        # セット (接続先・ワークフロー・長さをまとめて呼び出す)
        self._sets = comfy.sets()
        set_row = QHBoxLayout()
        set_row.addWidget(QLabel("セット"))
        self.set_box = QComboBox()
        for i, s in enumerate(self._sets):
            self.set_box.addItem(s.label(i))
        self.set_box.setCurrentIndex(-1)
        set_row.addWidget(self.set_box, 1)
        self.save_set = QPushButton("保存")
        self.save_set.setToolTip(
            "今の接続先・ワークフロー・長さ・" + ("読み取り・" if pf.reads else "") + "エンハンサー・差し込みを、選んでいる番号にまとめて保存します"
        )
        self.delete_set = QPushButton("削除")
        set_row.addWidget(self.save_set)
        set_row.addWidget(self.delete_set)
        layout.addLayout(set_row)
        # 前回選んでいたセットを選んだ状態にする (欄の中身はそこから入れ直さない)
        last = carry.get("set", comfy.last_set())
        if 0 <= last < len(self._sets) and not self._sets[last].is_empty:
            self.set_box.setCurrentIndex(last)
        self.save_set.setEnabled(self.set_box.currentIndex() >= 0)
        self.delete_set.setEnabled(self.set_box.currentIndex() >= 0)

        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {_c().separator};")
        layout.addWidget(line)

        form = QFormLayout()
        # 接続先
        self._conn_index, self._conn_url, self._conn_name = [], [], []
        self.conn_box = QComboBox()
        active = comfy.active_connection()
        for i, p in enumerate(comfy.connections()):
            if p.is_empty:
                continue
            self.conn_box.addItem(p.label(i))
            self._conn_index.append(i)
            self._conn_url.append(p.value)
            self._conn_name.append(p.name)
            if i == active:
                self.conn_box.setCurrentIndex(self.conn_box.count() - 1)
        if self.conn_box.count() == 0:
            self.conn_box.addItem(comfy.DEFAULT_URL)
            self._conn_index.append(-1)
            self._conn_url.append(comfy.DEFAULT_URL)
            self._conn_name.append("")
        if self.conn_box.currentIndex() < 0:
            self.conn_box.setCurrentIndex(0)
        form.addRow("接続先", self.conn_box)

        # ワークフロー (置いてあるフォルダで、どの台用かを見分けて名前の後ろに出す)
        self._all_conns = comfy.connections()
        self._folders = comfy.connection_folders()
        self._wf_index, self._wf_path, self._wf_name = [], [], []
        self.wf_box = QComboBox()
        self.wf_box.setMaxVisibleItems(20)
        active_wf = comfy.active_workflow()
        for i, p in enumerate(comfy.workflows()):
            if p.is_empty:
                continue
            machine = comfy.machine_name(p.value, self._all_conns, self._folders)
            self.wf_box.addItem(p.file_label(i) + comfy.machine_tag(p.name or comfy.file_name(p.value), machine))
            self._wf_index.append(i)
            self._wf_path.append(p.value)
            self._wf_name.append(p.name or comfy.file_name(p.value))
            if i == active_wf:
                self.wf_box.setCurrentIndex(self.wf_box.count() - 1)
        if self.wf_box.currentIndex() < 0 and self.wf_box.count() > 0:
            self.wf_box.setCurrentIndex(0)
        form.addRow("ワークフロー", self.wf_box)
        layout.addLayout(form)
        # 元画像も送るか (読み取りのあるアプリで、ワークフローに LoadImage があるか)
        self.source_hint = _hint("")
        layout.addWidget(self.source_hint)
        # ワークフローと接続先の台が違うとき
        self.machine_hint = QLabel()
        self.machine_hint.setWordWrap(True)
        self.machine_hint.setStyleSheet(f"color: {_c().paused};")
        self.machine_hint.setToolTip("ワークフローがどの台用かは、接続先ごとに決めたワークフローのフォルダで見分けています")
        self.machine_hint.hide()
        layout.addWidget(self.machine_hint)

        # 生成できない理由 (ワークフローを読めない、プロンプトを入れる場所がない、読み取りプリセットがない)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {_c().error};")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        # プロンプト (見せるだけ)
        self.prompt_label = QLabel()
        self.prompt_label.setWordWrap(True)
        layout.addWidget(self.prompt_label)
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setFixedHeight(140)
        if self.batch and pf.reads:
            done = len(captions) - self.unread
            self.prompt_label.setText(
                f"{pf.prompt_word} ({len(captions)} {pf.unit}: 読み取り済み {done} {pf.unit} / 未読み取り {self.unread} {pf.unit}。"
                "未読み取りのものは、ジョブの中で読み取ります)"
            )
        elif self.batch:
            self.prompt_label.setText(
                f"プロンプト (各{pf.source_word}の{pf.prompt_word}をそれぞれ使います: {len(captions)} {pf.unit})"
            )
        if self.batch:
            self.prompt_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.prompt_view.setPlainText("\n".join(
                f"{name} : {_one_line(text, 120) if text.strip() else '(未読み取り)'}" for name, text in captions
            ))
        elif self.unread:
            self.prompt_label.setText(f"{pf.prompt_word} ({captions[0][0]}): まだ読み取っていません。ジョブの中で読み取ります。")
            self.prompt_view.hide()
        else:
            where = " の .txt" if pf.reads else f" の{pf.prompt_word}"
            self.prompt_label.setText(f"{pf.prompt_word} ({captions[0][0]}{where}。書き換えるときは{pf.prompt_where}で)")
            self.prompt_view.setPlainText(captions[0][1])
        layout.addWidget(self.prompt_view)
        # 付け足すベースプロンプト。外すと、ベースプロンプトなしで送る (前回の状態を覚える)
        self.base_check = QCheckBox()
        self.base_check.setChecked(comfy.use_base_prompt())
        self.base_check.hide()
        layout.addWidget(self.base_check)

        # 読み取りプリセット (説明プロンプトがまだないもの・読み直すものに使う。読み取りのあるアプリだけ)
        self._readers = []
        self._reader_index: list[int] = []  # 欄の行 → 読み取りプリセットの番号
        reader_row = QHBoxLayout()
        reader_row.addWidget(QLabel("読み取り"))
        self.reader_box = QComboBox()
        self.reader_box.setMaxVisibleItems(20)
        reader_row.addWidget(self.reader_box, 1)
        self.reread_check = QCheckBox("読み取り済みでも読み直す")
        self.reread_check.setToolTip("オンにすると、説明プロンプトのある画像も読み取り直して .txt を上書きします")
        self.reread_check.setChecked(carry.get("reread", comfy.load("reader_reread.txt") == "1"))
        reader_row.addWidget(self.reread_check)
        self.reader_settings_button = reader_settings = QPushButton("設定...")
        reader_settings.setToolTip("読み取りの設定 (サーバーURL、モデル、読み取りプリセットの登録)")
        reader_settings.clicked.connect(self._reader_settings)
        reader_row.addWidget(reader_settings)
        if pf.reads:
            layout.addLayout(reader_row)
            self._fill_readers(reader.last_generate_index())
        else:
            for widget in (self.reader_box, self.reread_check, reader_settings):
                widget.hide()
            self.reread_check.setChecked(False)
        self._reader_note = ""  # 記録の読み取りプリセットが見つからなかったときの知らせ

        # エンハンサー (プロンプトを AI で盛ってから送る)
        self._enhancers = []
        self._enhancer_index = [-1]  # 欄の行 → エンハンサーの番号 (なしは -1)
        enhancer_row = QHBoxLayout()
        enhancer_row.addWidget(QLabel("エンハンサー"))
        self.enhancer_box = QComboBox()
        self.enhancer_box.setMaxVisibleItems(20)
        enhancer_row.addWidget(self.enhancer_box, 1)
        self.enhancer_settings_button = enhancer_settings = QPushButton("設定...")
        enhancer_settings.setToolTip("エンハンサーの設定 (指示文の登録、LLM のモデル)")
        enhancer_settings.clicked.connect(self._enhancer_settings)
        enhancer_row.addWidget(enhancer_settings)
        layout.addLayout(enhancer_row)
        self._fill_enhancers(enhancer.last_generate_index())
        self.enhancer_hint = _hint("")
        layout.addWidget(self.enhancer_hint)
        self._enhancer_note = ""  # 記録のエンハンサーが見つからなかったときの知らせ
        self.enhancer_box.currentIndexChanged.connect(self._show_enhancer_hint)

        # 差し込み (フォルダのテキストを、ジョブ 1 件ごとに 1 つずつ順に足す)
        self.insert_slots: list[inserts.Slot] = inserts.load_slots()  # 「生成する」で決まった枠 (呼び出し側が使う)
        self.insert_group = QGroupBox("差し込み (フォルダのテキストを、1 件ごとに順に足す。最後まで使ったら最初に戻る)")
        insert_layout = QVBoxLayout(self.insert_group)
        insert_layout.setContentsMargins(8, 4, 8, 4)
        self.insert_rows: list[InsertRow] = []
        for i, slot in enumerate(self.insert_slots):
            row = InsertRow(i, self.insert_group)
            row.set_slot(slot)
            row.changed.connect(self._update_error)
            insert_layout.addWidget(row)
            self.insert_rows.append(row)
        layout.addWidget(self.insert_group)

        # 長さ・回数
        seconds_row = QHBoxLayout()
        seconds_row.addWidget(QLabel("長さ"))
        self.seconds_edit = QLineEdit()
        self.seconds_edit.setFixedWidth(60)
        self.seconds_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        seconds_row.addWidget(self.seconds_edit)
        self.seconds_unit = QLabel()
        seconds_row.addWidget(self.seconds_unit)
        seconds_row.addStretch(1)
        layout.addLayout(seconds_row)

        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel("回数"))
        self.repeat_edit = QLineEdit(str(carry.get("repeat", "1")))
        self.repeat_edit.setFixedWidth(60)
        self.repeat_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        repeat_row.addWidget(self.repeat_edit)
        repeat_row.addWidget(QLabel(
            ("回ずつ" if self.batch else "回") + " (2 回以上にすると、seed を変えて繰り返します"
            + (f"。読み取りは 1 {pf.unit}につき 1 回)" if pf.reads else ")")
        ))
        repeat_row.addStretch(1)
        layout.addLayout(repeat_row)

        # ボタン
        bottom = QHBoxLayout()
        self.settings_button = settings_button = QPushButton("生成の設定...")
        settings_button.setToolTip(
            "接続先・ワークフロー・ベースプロンプト" + ("・出力フォルダ" if pf.output_folder else "") + "の登録や並び替え"
        )
        self.copy_button = QPushButton("コピー")
        self.copy_button.setToolTip(
            f"今の接続先・ワークフロー・長さ (1 {pf.unit}のときは{pf.prompt_word}も) をクリップボードにコピーします"
        )
        self.paste_button = paste_button = QPushButton("貼り付け")
        paste_button.setToolTip(
            f"コピーした生成パラメーターの接続先・ワークフロー・長さを各欄に入れます (プロンプトは{pf.prompt_word}のまま)"
        )
        bottom.addWidget(settings_button)
        bottom.addSpacing(12)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(paste_button)
        bottom.addStretch(1)
        self.view_button = QPushButton("図で開く")
        self.view_button.setToolTip("設定・処理・出力を図のように並べた画面に切り替えます (入力した内容はそのまま)")
        self.view_button.setAutoDefault(False)
        self.view_button.clicked.connect(self._switch_view)
        bottom.addWidget(self.view_button)
        bottom.addSpacing(12)
        self.ok_button = QPushButton(f"{len(captions)} {pf.unit}を生成する" if self.batch else "生成する")
        self.ok_button.setAutoDefault(False)
        self.cancel_button = cancel = QPushButton("キャンセル")
        cancel.setAutoDefault(False)
        bottom.addWidget(self.ok_button)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        self.ok_button.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        settings_button.clicked.connect(self._open_settings)
        self.copy_button.clicked.connect(lambda: copy_params(self.copy_button, "コピー", self._fields()))
        paste_button.clicked.connect(self._paste)
        # 開始は「生成する」ボタンか Ctrl+Enter (どの欄にいても効く)
        for key in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(key), self).activated.connect(self._accept)

        self._apply_workflow()
        self.wf_box.currentIndexChanged.connect(self._apply_workflow)
        self.reread_check.toggled.connect(self._update_error)
        self.set_box.currentIndexChanged.connect(self._on_set)
        # ワークフローを (人が) 選んだら、その台の接続先にする。セットや前回の値で選んだときは変えない
        self.wf_box.activated.connect(self._match_connection)
        self.wf_box.currentIndexChanged.connect(lambda _i: self._update_machine_hint())
        self.conn_box.currentIndexChanged.connect(lambda _i: self._update_machine_hint())
        self.save_set.clicked.connect(self._save_set)
        self.delete_set.clicked.connect(self._delete_set)

        if prefill is not None:
            self._apply(
                prefill.url, prefill.workflow_path,
                "" if prefill.seconds is None else comfy.format_seconds(prefill.seconds),
            )
            self._apply_reader(prefill.reader)
            self._apply_enhancer(prefill.enhancer)
            # 記録に差し込みがなければ、そのときは使っていない (入力中の内容は欄の形で持つ)
            self._apply_inserts(prefill.inserts if prefill.inserts is not None else [])
        self._show_enhancer_hint()
        self._update_machine_hint()

        if self.seconds_edit.isEnabled():
            self.seconds_edit.setFocus()
            self.seconds_edit.selectAll()
        else:
            self.ok_button.setFocus()

    # --- 欄の出し入れ -------------------------------------------------
    # --- ワークフローの台と接続先 -----------------------------------------
    def _match_connection(self, index: int):
        """ワークフローがある台用なら、接続先をその台にする (今の接続先がその台ならそのまま)。"""
        if not 0 <= index < len(self._wf_path):
            return
        machines = comfy.workflow_machines(self._wf_path[index], self._all_conns, self._folders)
        urls = {comfy._url_key(c.value) for c in machines}
        current = self.conn_box.currentIndex()
        if not urls or (0 <= current < len(self._conn_url) and comfy._url_key(self._conn_url[current]) in urls):
            return
        for i, url in enumerate(self._conn_url):
            if comfy._url_key(url) in urls:
                self.conn_box.setCurrentIndex(i)
                return

    def machine_conflict(self, url: str, path: str) -> str:
        return comfy.machine_conflict(url, path, self._all_conns, self._folders)

    def _update_machine_hint(self):
        wf, conn = self.wf_box.currentIndex(), self.conn_box.currentIndex()
        machine = ""
        if 0 <= wf < len(self._wf_path) and 0 <= conn < len(self._conn_url):
            machine = self.machine_conflict(self._conn_url[conn], self._wf_path[wf])
        self.machine_hint.setText(
            f"⚠ このワークフローは {machine} 用です。接続先の台と違います"
            if machine else "")
        self.machine_hint.setVisible(bool(machine))

    def _apply(self, url: str, workflow: str, seconds: str):
        """接続先・ワークフローは一覧にあるものだけ選び直す。"""
        for i, u in enumerate(self._conn_url):
            if u.lower() == (url or "").lower():
                self.conn_box.setCurrentIndex(i)
                break
        for i, p in enumerate(self._wf_path):
            if p.lower() == (workflow or "").lower():
                self.wf_box.setCurrentIndex(i)  # 使えるかどうかは _apply_workflow が確かめる
                break
        if self.seconds_edit.isEnabled() and _parse_seconds(seconds) is not None:
            self.seconds_edit.setText(seconds.strip())

    def _fill_readers(self, index: int):
        """読み取りプリセットを読み直して欄に並べ、index 番を選ぶ (設定から戻ったときも)。"""
        self._readers = reader.load_presets()
        self._reader_index = []
        self.reader_box.blockSignals(True)
        self.reader_box.clear()
        for i, preset in enumerate(self._readers):
            if not preset.is_empty:
                self.reader_box.addItem(preset.label(i))
                self._reader_index.append(i)
        self.reader_box.setEnabled(bool(self._reader_index))
        if not self._reader_index:
            self.reader_box.addItem("(読み取りプリセットが未登録です。「設定...」で登録してください)")
        self._select_reader(index)
        self.reader_box.blockSignals(False)

    def _fill_enhancers(self, index: int):
        """エンハンサーを読み直して欄に並べ、index 番 (なしは -1) を選ぶ。"""
        self._enhancers = enhancer.load_presets()
        self._enhancer_index = [-1]
        self.enhancer_box.blockSignals(True)
        self.enhancer_box.clear()
        self.enhancer_box.addItem(f"なし ({self.profile.prompt_word}をそのまま送る)")
        for i, preset in enumerate(self._enhancers):
            if not preset.is_empty:
                self.enhancer_box.addItem(preset.label(i))
                self._enhancer_index.append(i)
        self._select_enhancer(index)
        self.enhancer_box.blockSignals(False)

    def _reader_settings(self):
        current = self._current_reader()
        dialog = reader.ReaderSettingsDialog(max(current, 0), self)
        dialog.exec()  # プリセットはその場で保存されるので、キャンセルでも読み直す
        self._fill_readers(current)
        self._update_error()

    def _enhancer_settings(self):
        current = self._current_enhancer()
        dialog = enhancer.EnhancerSettingsDialog(max(current, 0), self)
        dialog.exec()
        self._fill_enhancers(current)
        self._show_enhancer_hint()

    def _select_reader(self, index: int) -> bool:
        if index in self._reader_index:
            self.reader_box.setCurrentIndex(self._reader_index.index(index))
            return True
        if self._reader_index:
            self.reader_box.setCurrentIndex(0)
        return False

    def _apply_reader(self, snap: Optional[dict]):
        """記録や入力中の内容の読み取りプリセットを選ぶ。番号が分かればそれ、なければ名前で探す。"""
        self._reader_note = ""
        if snap is None:
            return
        index = snap.get("index")
        if isinstance(index, int) and self._select_reader(index):
            return
        name = str(snap.get("name", ""))
        found = next((i for i, p in enumerate(self._readers) if name and p.name == name and not p.is_empty), -1)
        if not self._select_reader(found) and name:
            self._reader_note = f"記録の読み取りプリセット「{name}」は登録にないので、別のものを選びました。"

    def _current_reader(self) -> int:
        if not self.profile.reads or not self._reader_index:
            return -1
        return self._reader_index[max(self.reader_box.currentIndex(), 0)]

    def _select_enhancer(self, index: int) -> bool:
        if index in self._enhancer_index:
            self.enhancer_box.setCurrentIndex(self._enhancer_index.index(index))
            return True
        self.enhancer_box.setCurrentIndex(0)
        return False

    def _apply_enhancer(self, snap: Optional[dict]):
        """記録や入力中の内容のエンハンサーを選ぶ。番号が分かればそれ、なければ名前で探す。"""
        self._enhancer_note = ""
        if snap is None:
            self._select_enhancer(-1)
            return
        index = snap.get("index")
        if isinstance(index, int) and self._select_enhancer(index):
            return
        name = str(snap.get("name", ""))
        found = next((i for i, p in enumerate(self._enhancers) if name and p.name == name and not p.is_empty), -1)
        self._select_enhancer(found)
        if found < 0:
            self._enhancer_note = f"記録のエンハンサー「{name}」は登録にないので、「なし」にしました。"

    def _current_enhancer(self) -> int:
        return self._enhancer_index[max(self.enhancer_box.currentIndex(), 0)]

    def _show_enhancer_hint(self):
        notes = [self._reader_note] if self._reader_note else []
        if self._current_enhancer() >= 0:
            self._enhancer_note = ""
            notes.append(
                f"{self.profile.prompt_word}を AI で盛ってから送ります (ベースプロンプトは盛りません)。"
                "ジョブの番が来るたびに盛るので、回数を増やすと毎回違う盛り方になります。"
            )
        elif self._enhancer_note:
            notes.append(self._enhancer_note)
        self.enhancer_hint.setText("\n".join(notes))
        self.enhancer_hint.setVisible(bool(notes))

    def _fields(self) -> comfy.GenRequest:
        """今の各欄の内容 (長さは、使えないときや正しくないときは None)。1 件のときはプロンプトも入れる。"""
        sel = max(self.conn_box.currentIndex(), 0)
        r = comfy.GenRequest(url=self._conn_url[sel], connection_name=self._conn_name[sel])
        wf = self.wf_box.currentIndex()
        if wf >= 0:
            r.workflow_path = self._wf_path[wf]
            r.workflow_name = self._wf_name[wf]
        if not self.batch and self.captions[0][1].strip():
            r.prompt = self.captions[0][1]
        if self.seconds_edit.isEnabled():
            r.seconds = _parse_seconds(self.seconds_edit.text())
        index = self._current_reader()
        if index >= 0:
            r.reader = {"name": self._readers[index].name, "index": index}
        index = self._current_enhancer()
        if index >= 0:
            r.enhancer = {"name": self._enhancers[index].name, "index": index}
        r.inserts = inserts.to_data(self._current_slots())
        return r

    # --- 差し込み -----------------------------------------------------
    def _current_slots(self) -> list[inserts.Slot]:
        return [row.slot() for row in self.insert_rows]

    def _apply_inserts(self, data):
        for row, slot in zip(self.insert_rows, inserts.apply(self._current_slots(), data)):
            row.set_slot(slot)

    def _apply_workflow(self):
        """選んでいるワークフローを読んで、プロンプトを渡せるか・長さを変えられるか・元画像を送るかを決める。

        プロンプトを入れる場所がないワークフローでは「生成する」を押せなくする。
        """
        video = False
        wf_seconds = None
        error = None
        self._integer_seconds = False
        self.source_hint.setText("")
        try:
            sel = self.wf_box.currentIndex()
            if sel < 0:
                raise comfy.ComfyError("ワークフローが登録されていません。「生成の設定...」で登録してください。")
            wf = comfy.load_workflow(self._wf_path[sel])
            problem = comfy.prompt_slot_problem(wf)
            if problem:
                raise comfy.ComfyError(problem)
            video = comfy.is_video_workflow(wf)
            self._wf_data = wf
            wf_seconds = comfy.get_duration(wf)
            self._integer_seconds = comfy.duration_is_integer(wf)
            if self.profile.reads:
                self.source_hint.setText(
                    "元画像も渡します (ワークフローに「Load Image」があります)" if comfy.uses_source_image(wf)
                    else "元画像は渡さず、プロンプトだけで生成します"
                )
        except comfy.ComfyError as exc:
            error = str(exc)
            self._wf_data = None
        self._workflow_error = error
        self.source_hint.setVisible(bool(self.source_hint.text()))

        ok = error is None
        self._base_prompt = comfy.base_prompt_for(video) if ok else ""
        kind = "動画用" if video else "画像用"
        one_line = self._base_prompt.replace("\r", "").replace("\n", " ")
        self.base_check.setText(_shorten(f"＋ ベースプロンプト ({kind}) : {one_line}", 90))
        self.base_check.setToolTip(
            self._base_prompt + "\n\n(チェックを外すと、ベースプロンプトを付けずに送ります)"
        )
        self.base_check.setVisible(bool(self._base_prompt))

        has_seconds = wf_seconds is not None
        if has_seconds and _parse_seconds(self.seconds_edit.text()) is None:
            # 前回の長さ、なければワークフローに書かれている長さ
            last = comfy.last_seconds()
            self.seconds_edit.setText(last if _parse_seconds(last) is not None else comfy.format_seconds(wf_seconds))
        self.seconds_edit.setEnabled(has_seconds)
        self.seconds_unit.setText("秒" if has_seconds or not ok else "秒 (このワークフローでは変えられません)")
        self.seconds_unit.setStyleSheet("" if has_seconds else f"color: {_c().hint};")
        self._update_error()

    def _update_error(self):
        """生成できない理由を出し、「生成する」を押せるかを決める。"""
        pf = self.profile
        error = self._workflow_error
        if (error is None and pf.reads and (self.unread or self.reread_check.isChecked())
                and self._current_reader() < 0):
            error = "読み取りプリセットが登録されていないので、説明プロンプトを読み取れません。「設定...」で登録してください。"
        if error is None and pf.skip_empty and not self.batch and not self.captions[0][1].strip():
            error = f"{pf.source_word}の{pf.prompt_word}が空です。{pf.prompt_word}を書いてから生成してください。"
        if error is None:
            for row in getattr(self, "insert_rows", []):
                if row.problem:
                    error = f"差し込み「{row.slot().label(row.index)}」: {row.problem}"
                    break
        self.error_label.setText(error or "")
        self.error_label.setVisible(error is not None)
        self.ok_button.setEnabled(error is None)

    # --- セット -------------------------------------------------------
    def _on_set(self, n: int):
        if self._relabeling_set:
            return
        self.save_set.setEnabled(n >= 0)
        self.delete_set.setEnabled(n >= 0 and not self._sets[n].is_empty)
        if n < 0 or self._sets[n].is_empty:
            return
        s = self._sets[n]
        self._apply(s.url, s.workflow, s.seconds)  # セットのプロンプト (J-Rate から持ってきたもの) は使わない
        # 読み取りとエンハンサーは名前で選ぶ (その項目のないセットでは、今のまま)
        notes = []
        if self.profile.reads and s.reader:
            found = self._find_by_name(self._readers, s.reader)
            if not self._select_reader(found):
                notes.append(f"セットの読み取りプリセット「{s.reader}」は登録にないので、別のものを選びました。")
        if self.profile.reads and s.reread is not None:
            self.reread_check.setChecked(s.reread)
        if s.enhancer is not None:
            found = self._find_by_name(self._enhancers, s.enhancer) if s.enhancer else -1
            self._select_enhancer(found)
            if s.enhancer and found < 0:
                notes.append(f"セットのエンハンサー「{s.enhancer}」は登録にないので、「なし」にしました。")
        if s.inserts is not None:
            self._apply_inserts(s.inserts)
        self._reader_note = "\n".join(notes)
        self._enhancer_note = ""
        self._show_enhancer_hint()
        self._update_error()

    @staticmethod
    def _find_by_name(items, name: str) -> int:
        return next((i for i, p in enumerate(items) if name and p.name == name and not p.is_empty), -1)

    def _relabel_set(self, n: int):
        self._relabeling_set = True
        self.set_box.setItemText(n, self._sets[n].label(n))
        self.set_box.setCurrentIndex(n)
        self._relabeling_set = False
        self.delete_set.setEnabled(not self._sets[n].is_empty)

    def _save_set(self):
        n = self.set_box.currentIndex()
        wf = self.wf_box.currentIndex()
        if n < 0 or wf < 0:
            return
        s = self._sets[n]
        name, ok = QInputDialog.getText(
            self, "セットを保存", f"セット {n + 1} の名前", text=s.name or self._wf_name[wf]
        )
        if not ok:
            return
        s.name = name.strip()
        s.url = self._conn_url[max(self.conn_box.currentIndex(), 0)]
        s.workflow = self._wf_path[wf]
        s.prompt = ""
        s.prompt_preset = -1
        s.seconds = self.seconds_edit.text().strip() if self.seconds_edit.isEnabled() else ""
        if self.profile.reads:
            index = self._current_reader()
            s.reader = self._readers[index].name if index >= 0 else ""
            s.reread = self.reread_check.isChecked()
        index = self._current_enhancer()
        s.enhancer = self._enhancers[index].name if index >= 0 else ""
        s.inserts = inserts.to_data(self._current_slots())
        comfy.set_sets(self._sets)
        self._relabel_set(n)

    def _delete_set(self):
        n = self.set_box.currentIndex()
        if QMessageBox.question(
            self, self.windowTitle(), f"セット {n + 1} を削除しますか？\n\n{self._sets[n].name}"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._sets[n] = comfy.ComfySet()
        comfy.set_sets(self._sets)
        self._relabel_set(n)

    # --- ボタン -------------------------------------------------------
    def _open_settings(self, tab: str = ""):
        self.settings_tab = tab if isinstance(tab, str) else ""  # clicked から呼ぶと bool が来る
        self.reopen = self._fields()
        comfy.set_use_base_prompt(self.base_check.isChecked())  # 開き直したときも同じ状態に
        self.reject()

    def _switch_view(self):
        """いつもの画面と図の画面を切り替える。入力した内容を持って閉じ、呼び出し側がもう一方で開き直す。"""
        self.reopen = self._fields()
        self.carry = {"repeat": self.repeat_edit.text().strip() or "1", "set": self.set_box.currentIndex(),
                      "reread": self.reread_check.isChecked()}
        comfy.set_use_base_prompt(self.base_check.isChecked())
        self.switch_view = True
        self.reject()

    def _paste(self):
        r = paste_params()
        if r is None:
            QMessageBox.information(self, self.windowTitle(), "クリップボードに、コピーした生成パラメーターがありません。")
            return
        missing = []
        if r.url and not any(u.lower() == r.url.lower() for u in self._conn_url):
            missing.append("接続先 : " + (f"{r.connection_name}  ({r.url})" if r.connection_name else r.url))
        wf = r.workflow_path
        if wf and not any(p.lower() == wf.lower() for p in self._wf_path):
            # パスが違っても同じファイル名のものがあればそれ (別の PC でコピーしたとき用)
            name = comfy.file_name(wf).lower()
            same = next((p for p in self._wf_path if comfy.file_name(p).lower() == name), None)
            if same:
                wf = same
            else:
                label = f"{r.workflow_name}  ({wf})" if r.workflow_name and r.workflow_name.lower() != name else wf
                missing.append("ワークフロー : " + label)
        self._apply(r.url, wf, "" if r.seconds is None else comfy.format_seconds(r.seconds))
        if missing:
            QMessageBox.information(
                self, self.windowTitle(),
                "次のものは登録されていないので、今の選択のままにしました。\n\n" + "\n".join(missing),
            )

    def _accept(self):
        if not self.ok_button.isEnabled():
            return  # 生成できない状態 (Ctrl+Enter で来たとき)
        try:
            repeat = int(self.repeat_edit.text().strip())
        except ValueError:
            repeat = 0
        if not 1 <= repeat <= MAX_REPEAT:
            QMessageBox.warning(self, self.windowTitle(), f"回数は 1 ～ {MAX_REPEAT} の整数で指定してください。")
            self.repeat_edit.setFocus()
            self.repeat_edit.selectAll()
            return

        # プロンプトは呼び出し側がファイルごとに入れる。ここではベースプロンプトまで
        use_base = self.base_check.isChecked()
        request = comfy.GenRequest(base_prompt=self._base_prompt if use_base else "")
        if self.seconds_edit.isEnabled():
            seconds = _parse_seconds(self.seconds_edit.text())
            if seconds is None or (self._integer_seconds and seconds != int(seconds)):
                kind = "整数" if self._integer_seconds else "数字"
                QMessageBox.warning(self, self.windowTitle(), f"長さは 1 ～ {MAX_SECONDS} 秒の{kind}で指定してください。")
                self.seconds_edit.setFocus()
                self.seconds_edit.selectAll()
                return
            request.seconds = seconds
            comfy.set_last_seconds(self.seconds_edit.text())
        sel = max(self.conn_box.currentIndex(), 0)
        request.url = self._conn_url[sel]
        request.connection_name = self._conn_name[sel]
        if self._conn_index[sel] >= 0:
            comfy.set_active_connection(self._conn_index[sel])
        wf = self.wf_box.currentIndex()
        request.workflow_path = self._wf_path[wf]
        request.workflow_name = self._wf_name[wf]
        comfy.set_active_workflow(self._wf_index[wf])
        comfy.set_last_set(self.set_box.currentIndex())
        if self.profile.reads:
            index = self._current_reader()
            self.reader_preset = self._readers[index] if index >= 0 else None
            if index >= 0:
                reader.set_last_generate_index(index)
            self.reread = self.reread_check.isChecked()
            comfy.save("reader_reread.txt", "1" if self.reread else "0")
        index = self._current_enhancer()
        self.enhancer_preset = self._enhancers[index] if index >= 0 else None
        enhancer.set_last_generate_index(index)
        self.insert_slots = self._current_slots()
        inserts.save_slots(self.insert_slots)
        comfy.set_use_base_prompt(use_base)
        self.request = request
        self.repeat = repeat
        self.accept()


def _one_line(text: str, limit: int) -> str:
    return _shorten(" ".join(line.strip() for line in text.splitlines() if line.strip()), limit)


def _parse_seconds(text: str) -> Optional[float]:
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if 1 <= value <= MAX_SECONDS else None


# ----------------------------------------------------------------------
# 生成ジョブの一覧 (閉じてもジョブは続く)
# ----------------------------------------------------------------------
def _clock(stamp: Optional[float]) -> str:
    """時刻 (time.time) を短く。今日なら時:分:秒、ほかの日なら月/日も付ける。"""
    if not stamp:
        return ""
    from datetime import datetime
    when = datetime.fromtimestamp(stamp)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M:%S")
    return f"{when.month}/{when.day} " + when.strftime("%H:%M:%S")


def _job_times(job) -> str:
    """生成時刻の欄のヒント: 登録・開始・終了の時刻。"""
    lines = [f"登録: {_clock(job.queued_at)}"]
    if job.started_at:
        lines.append(f"生成開始: {_clock(job.started_at)}")
    if job.finished_at:
        lines.append(f"終了: {_clock(job.finished_at)}")
    return "\n".join(lines)


class JobWindow(QWidget):
    def __init__(self, manager, cancel: Callable, show: Callable, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        pf = app.profile()
        self.COLUMNS = (pf.column, "ワークフロー", "状態", "生成時刻", "時間", "内容")
        self.setWindowTitle(f"生成ジョブ - {pf.name}")
        self.resize(840, 420)
        self.manager = manager
        self._cancel = cancel
        self._show = show
        self._items = {}

        layout = QVBoxLayout(self)
        # 今どれを処理しているかを、一覧をスクロールしても見えるように上に出す
        self.running_label = QLabel()
        self.running_label.setWordWrap(True)
        layout.addWidget(self.running_label)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.tree.header()
        for column, width in enumerate((190, 140, 70, 70, 60)):
            header.resizeSection(column, width)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("一時停止")
        self.pause_button.setToolTip(
            "新しく ComfyUI に依頼するのを止めます (実行中のものは最後まで生成します)。\n"
            "一時停止のままアプリを閉じると、次に起動したときも一時停止のまま残ります。\n"
            "ワークフローは依頼するときに読み直すので、直してから再開できます。"
        )
        self.cancel_button = QPushButton("選択したものをキャンセル")
        self.cancel_all_button = QPushButton("すべてキャンセル")
        self.show_button = QPushButton("結果を表示")
        self.clear_button = QPushButton("終わったものを消す")
        for button in (self.pause_button, self.cancel_button, self.cancel_all_button,
                       self.show_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton("閉じる")
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.cancel_all_button.clicked.connect(self._cancel_all)
        self.show_button.clicked.connect(self._show_selected)
        self.clear_button.clicked.connect(self._clear_finished)
        close.clicked.connect(self.close)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._show_selected())
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

        self.refresh()

    def _selected_jobs(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]

    def refresh(self):
        """ジョブの一覧と表示を合わせる。"""
        jobs = self.manager.jobs
        alive = {id(job) for job in jobs}
        for key in [k for k in self._items if k not in alive]:
            item = self._items.pop(key)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        newly_running = None
        for job in jobs:
            item = self._items.get(id(job))
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, job)
                self.tree.addTopLevelItem(item)
                self._items[id(job)] = item
            item.setText(0, job.file_name)
            item.setText(1, job.request.workflow_name)
            paused = self.manager.paused and job.is_active and not job.is_running
            item.setText(2, "一時停止" if paused else job.state_text)
            item.setText(3, _clock(job.finished_at))  # 生成が終わった時刻
            item.setToolTip(3, _job_times(job))
            item.setText(4, "" if job.run_time is None else comfy.format_elapsed(job.run_time))
            item.setText(5, job.status)
            item.setToolTip(5, job.status)
            # 実行中の行は色を付けて太字に
            was_running = bool(item.data(1, Qt.ItemDataRole.UserRole))
            if job.is_running != was_running:
                item.setData(1, Qt.ItemDataRole.UserRole, job.is_running)
                font = item.font(0)
                font.setBold(job.is_running)
                for column in range(len(self.COLUMNS)):
                    item.setFont(column, font)
                    item.setBackground(column, QBrush(RUNNING_BACK) if job.is_running else QBrush())
                if job.is_running and newly_running is None:
                    newly_running = item
        if newly_running is not None:
            # 新しく実行に移ったものが見えるようにする (見えていればスクロールしない)
            self.tree.scrollToItem(newly_running)

        running = [j for j in jobs if j.is_running]
        waiting = sum(1 for j in jobs if j.is_active and not j.is_running)
        if self.manager.paused:
            lines = [f"実行中: {j.file_name}  {j.status}" for j in running]
            if running:
                lines.append(f"一時停止中: 実行中のものが終わったら止まります (待機 {waiting} 件)")
            else:
                lines.append(f"一時停止中 (待機 {waiting} 件)。「再開」で続きを生成します")
            self.running_label.setText("\n".join(lines))
            self.running_label.setStyleSheet(f"color: {_c().paused}; font-weight: bold;")
        elif running:
            lines = [f"実行中: {j.file_name}  {j.status}" for j in running]
            if waiting:
                lines.append(f"待機 {waiting} 件")
            self.running_label.setText("\n".join(lines))
            self.running_label.setStyleSheet(f"color: {_c().ok}; font-weight: bold;")
        else:
            self.running_label.setText("実行中のジョブはありません")
            self.running_label.setStyleSheet(f"color: {_c().hint};")
        self.update_buttons()

    def update_buttons(self):
        selected = self._selected_jobs()
        jobs = self.manager.jobs
        self.pause_button.setText("再開" if self.manager.paused else "一時停止")
        self.pause_button.setEnabled(any(j.is_active for j in jobs))
        self.cancel_button.setEnabled(any(j.is_active for j in selected))
        self.cancel_all_button.setEnabled(any(j.is_active for j in jobs))
        self.show_button.setEnabled(len(selected) == 1 and bool(selected[0].outputs))
        self.clear_button.setEnabled(any(not j.is_active for j in jobs))

    def _toggle_pause(self):
        self.manager.set_paused(not self.manager.paused)
        self.refresh()

    def _confirm_cancel(self, jobs, what: str) -> bool:
        """キャンセルする前に確かめる (待機中・実行中のものだけ数える)。誤って押したときのため、既定は「いいえ」。"""
        targets = [j for j in jobs if j.is_active]
        if not targets:
            return False
        running = sum(1 for j in targets if j.is_running)
        if len(targets) == 1:
            text = f"このジョブをキャンセルしますか？\n\n{targets[0].file_name}"
        else:
            text = f"{what} {len(targets)} 件のジョブをキャンセルしますか？"
        if running:
            text += f"\n\n実行中のもの ({running} 件) は ComfyUI に中断を送ります。"
        answer = QMessageBox.question(
            self, "ジョブをキャンセル", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _cancel_selected(self):
        selected = self._selected_jobs()
        if not self._confirm_cancel(selected, "選択した"):
            return
        for job in selected:
            self._cancel(job)
        self.refresh()

    def _cancel_all(self):
        jobs = list(self.manager.jobs)
        if not self._confirm_cancel(jobs, "待機中・実行中の"):
            return
        for job in jobs:
            self._cancel(job)
        self.refresh()

    def _show_selected(self):
        selected = self._selected_jobs()
        if len(selected) == 1 and selected[0].outputs:
            self._show(selected[0])

    def _clear_finished(self):
        self.manager.jobs[:] = [j for j in self.manager.jobs if j.is_active]
        self.refresh()


# ----------------------------------------------------------------------
# 生成時の設定
# ----------------------------------------------------------------------
class GenInfoDialog(QDialog):
    def __init__(self, file_name: str, record: comfy.GenRecord, current_text: Optional[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"生成時の設定 - {file_name}")
        self.resize(620, 0)
        self.regenerate = False  # 「この設定で生成する...」が押されたら True

        req = record.request
        has_source = bool(record.source) and Path(record.source).is_file()
        layout = QVBoxLayout(self)
        form = QFormLayout()

        def row(caption, value):
            # 選んでコピーできるように、読み取り専用の欄で出す
            field = QLineEdit(value)
            field.setReadOnly(True)
            field.setFrame(False)
            field.setStyleSheet("background: transparent;")
            field.setCursorPosition(0)
            label = QLabel(caption)
            label.setStyleSheet(f"color: {_c().hint};")
            form.addRow(label, field)

        row("生成した日時", record.time)
        pf = app.profile()
        row(pf.column, record.source + ("" if has_source else "  (見つかりません)"))
        row("出来たファイル", record.output)
        row("接続先", f"{req.connection_name}  ({req.url})" if req.connection_name else req.url)
        same_name = req.workflow_name == comfy.file_name(req.workflow_path)
        row("ワークフロー", req.workflow_path if not req.workflow_name or same_name
            else f"{req.workflow_name}  ({req.workflow_path})")
        row("長さ", "(ワークフローのまま)" if req.seconds is None else f"{comfy.format_seconds(req.seconds)} 秒")
        if req.reader is not None:
            row("読み取りプリセット", req.reader_name or "(名前なし)")
        if req.enhancer is not None:
            row("エンハンサー", req.enhancer_name or "(名前なし)")
        if req.inserts:
            row("差し込み", inserts.summary(req.inserts))
        layout.addLayout(form)

        layout.addWidget(QLabel(f"{pf.prompt_word} (送った時点のもの)" if req.prompt is not None
                                else "プロンプト (ワークフローのまま)"))
        prompt = QPlainTextEdit(req.prompt or "")
        prompt.setReadOnly(True)
        prompt.setFixedHeight(120)
        layout.addWidget(prompt)
        if current_text is not None and req.prompt is not None and current_text.strip() != req.prompt.strip():
            layout.addWidget(_hint(
                f"※ {pf.prompt_word}は、このあと書き換えられています。やり直すときは今の{pf.prompt_word}を使います。"
            ))
        if req.enhanced_prompt is not None:
            layout.addWidget(QLabel("盛ったプロンプト (AI で盛って、実際に送った文。このあとにベースプロンプトを付けて送りました)"))
            enhanced = QPlainTextEdit(req.enhanced_prompt)
            enhanced.setReadOnly(True)
            enhanced.setFixedHeight(120)
            layout.addWidget(enhanced)
        for item in req.inserts or []:
            when = "盛る前に入れました" if item.get("before") else "そのまま付けました"
            if req.enhancer is None:
                when = "付けました"
            layout.addWidget(QLabel(
                f"差し込み「{item.get('name', '')}」 ({item.get('file', '')}。{item.get('folder', '')} から。{when})"))
            text = QPlainTextEdit(str(item.get("text") or ""))
            text.setReadOnly(True)
            text.setFixedHeight(60)
            layout.addWidget(text)
        if req.base_prompt:
            layout.addWidget(QLabel("ベースプロンプト (プロンプトのあとに続けて送りました)"))
            base = QPlainTextEdit(req.base_prompt)
            base.setReadOnly(True)
            base.setFixedHeight(70)
            layout.addWidget(base)

        bottom = QHBoxLayout()
        copy = QPushButton("パラメーターをコピー")
        copy.setToolTip("接続先・ワークフロー・プロンプト・長さをクリップボードにコピーします (「生成する」の画面で貼り付けられます)")
        copy.clicked.connect(lambda: copy_params(copy, "パラメーターをコピー", req))
        bottom.addWidget(copy)
        bottom.addStretch(1)
        again = QPushButton("この設定で生成する...")
        again.setEnabled(has_source)
        again.setToolTip(
            f"今の{pf.prompt_word}で、同じ接続先・ワークフロー・長さ・" + ("読み取りプリセット・" if pf.reads else "")
            + "エンハンサー・差し込みのフォルダでもう一度生成します (seed は変わります。エンハンサーを使ったときは、盛り直します。"
            + "差し込みは、そのフォルダの次のファイルを使います"
            + (f"。{pf.prompt_word}がなければ読み取ります)" if pf.reads else ")")
        )
        again.clicked.connect(self._again)
        close = QPushButton("閉じる")
        close.setDefault(True)
        close.clicked.connect(self.reject)
        bottom.addWidget(again)
        bottom.addWidget(close)
        layout.addLayout(bottom)

    def _again(self):
        self.regenerate = True
        self.accept()
