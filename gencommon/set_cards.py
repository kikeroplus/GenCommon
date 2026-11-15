"""セットを、中身の概要が見えるカードの横並び (横スクロール) で選ぶ部品。

図の画面の最上部で使う。選んでいる番号は呼び出し側 (GenerateDialog の set_box) が持ち、
カードをクリックすると on_select(番号) を呼ぶ。保存・削除のあとは refresh() で描き直す。
"""

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import comfy
from .comfy_dialogs import _c

CARD_WIDTH = 196
EMPTY_LINE = "#9a9a9a"  # 空きのカードの点線 (明るい・暗い配色どちらでも見える)
WARN = "#b7851f"  # 登録にないもの・読めないもの

# ワークフローの種類の色 (カードの左の帯と、種類の札)。明るい・暗い配色どちらでも見え、
# 図の画面の送る先の色 (薄紫・黄色) と重ならないもの
KIND_COLORS = {
    "T2I": "#3d8fe0",  # 青
    "I2I": "#2fa86a",  # 緑
    "I2I CN": "#8a4fd8",  # 紫 (ControlNet で元の写真の構図・ポーズを写す I2I)
    "T2V": "#e0862c",  # 橙
    "I2V": "#d24f8e",  # 赤紫
}
KIND_STRIPE = 6  # 左の帯の太さ


class SetInfo:
    """カードに出す 1 件分の概要 (呼び出し側が、登録の名前などから作る)。"""

    def __init__(self, title: str, kind: str, lines: list[str], tip: str, warn: bool = False, empty: bool = False,
                 wf_kind: str = ""):
        self.title = title
        self.kind = kind  # 「🎬 動画 5 秒」「🖼 画像」など
        self.lines = lines
        self.tip = tip
        self.warn = warn
        self.empty = empty
        self.wf_kind = wf_kind  # "T2I" / "I2I" / "I2I CN" / "T2V" / "I2V" (読めなければ空)


class _Card(QFrame):
    clicked = Signal(int)

    def __init__(self, index: int):
        super().__init__()
        self.index = index
        self.setObjectName("setcard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(CARD_WIDTH)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(5, 6, 8, 6)
        outer.setSpacing(6)
        self.stripe = QFrame()  # 左の帯 (ワークフローの種類の色)
        self.stripe.setFixedWidth(KIND_STRIPE)
        outer.addWidget(self.stripe)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        outer.addLayout(layout, 1)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.number = QLabel(str(index + 1))
        self.number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number.setFixedSize(22, 22)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        head.addWidget(self.number)
        head.addWidget(self.title, 1)
        layout.addLayout(head)
        kind_row = QHBoxLayout()
        kind_row.setSpacing(6)
        self.badge = QLabel()  # 種類の札 (T2I など)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.kind = QLabel()
        kind_row.addWidget(self.badge)
        kind_row.addWidget(self.kind, 1)
        layout.addLayout(kind_row)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        layout.addStretch(1)

    def show_info(self, info: SetInfo, selected: bool):
        metrics = self.title.fontMetrics()
        width = CARD_WIDTH - 50 - KIND_STRIPE - 6
        self.title.setText(metrics.elidedText(info.title, Qt.TextElideMode.ElideRight, width))
        self.kind.setText(info.kind)
        detail_width = CARD_WIDTH - 18 - KIND_STRIPE - 6
        self.detail.setText("\n".join(
            self.detail.fontMetrics().elidedText(line, Qt.TextElideMode.ElideMiddle, detail_width) for line in info.lines))
        self.setToolTip(info.tip)
        hint = _c().hint
        if selected:
            border = "2px solid palette(highlight)"
        elif info.empty:
            border = f"1.5px dashed {EMPTY_LINE}"
        else:
            border = "1px solid palette(mid)"
        color = KIND_COLORS.get(info.wf_kind, "")
        self.setStyleSheet(f"#setcard {{ border: {border}; border-radius: 8px; background: palette(base); }}")
        self.stripe.setStyleSheet(f"background: {color or 'transparent'}; border-radius: 3px;")
        self.badge.setVisible(bool(color))
        self.badge.setText(info.wf_kind)
        self.badge.setStyleSheet(
            f"background: {color}; color: white; font-weight: bold; border-radius: 4px; padding: 0px 5px;")
        badge = "palette(highlight)" if selected else ("transparent" if info.empty else "palette(mid)")
        number_color = "palette(highlighted-text)" if selected else (hint if info.empty else "palette(base)")
        self.number.setStyleSheet(
            f"border-radius: 11px; background: {badge}; color: {number_color}; font-weight: bold;"
            + (f" border: 1px dashed {EMPTY_LINE};" if info.empty and not selected else ""))
        self.title.setStyleSheet("font-weight: bold;" + (f" color: {hint};" if info.empty else ""))
        self.kind.setStyleSheet(f"color: {WARN};" if info.warn else "")
        self.detail.setStyleSheet(f"color: {hint};")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)


class _Scroll(QScrollArea):
    """縦のホイールでも横に送る。"""

    def wheelEvent(self, event):
        delta = event.angleDelta()
        step = delta.y() if delta.y() else delta.x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - step)
        event.accept()


class SetStrip(QWidget):
    """セットのカードを横に並べ、横にスクロールできる帯。"""

    def __init__(self, count: int, on_select: Callable[[int], None], parent=None):
        super().__init__(parent)
        self._on_select = on_select
        self.scroll = _Scroll()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inner = QWidget()
        row = QHBoxLayout(inner)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        self.cards: list[_Card] = []
        for i in range(count):
            card = _Card(i)
            card.clicked.connect(self._on_select)
            row.addWidget(card)
            self.cards.append(card)
        row.addStretch(1)
        self.scroll.setWidget(inner)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

    def refresh(self, infos: list[SetInfo], current: int):
        for card, info in zip(self.cards, infos):
            card.show_info(info, card.index == current)
        # カードの高さをそろえ、帯の高さはカード + 横スクロールバー
        for card in self.cards:
            card.layout().activate()  # 内側の並びの大きさを、入れた文字に合わせて計算し直す
        height = max(card.sizeHint().height() for card in self.cards)
        for card in self.cards:
            card.setFixedHeight(height)
        self.scroll.setFixedHeight(height + 4 + self.scroll.horizontalScrollBar().sizeHint().height())
        if 0 <= current < len(self.cards):
            self.scroll.ensureWidgetVisible(self.cards[current], 16, 0)


def _insert_names(s: comfy.ComfySet) -> list[str]:
    return [str(d.get("name") or "") for d in (s.inserts or []) if isinstance(d, dict) and d.get("use")]


def describe_sets(sets: list[comfy.ComfySet], connections: list[tuple[str, str]], workflows: list[tuple[str, str]],
                  reads: bool, cache: Optional[dict] = None,
                  conflict: Optional[Callable[[str, str], str]] = None) -> list[SetInfo]:
    """セットの概要。connections / workflows は登録の (名前, URL・パス)。cache はワークフローの読み取り結果を覚えておく辞書。

    conflict(URL, パス) は、ワークフローが別の台用ならその台の名前を返す (台の違いを注意として出す)。
    """
    cache = {} if cache is None else cache
    conn_names = {url.lower(): name for name, url in connections if url}
    wf_names = {path.lower(): name for name, path in workflows if path}
    infos = []
    for s in sets:
        if s.is_empty:
            infos.append(SetInfo("空き", "", ["保存すると、今の設定を", "ここに入れます"],
                                 "空きの番号です。選んで「保存」を押すと、今の設定をこの番号に保存します。", empty=True))
            continue
        warn = False
        notes = []
        # 種類 (T2I / I2I / T2V / I2V。ワークフローの中身で見分ける)
        key = s.workflow.lower()
        if key not in cache:
            try:
                wf = comfy.load_workflow(s.workflow)
                cache[key] = comfy.workflow_kind(wf)
                if cache[key] == "I2I" and comfy.uses_controlnet(wf):
                    cache[key] = "I2I CN"
            except (comfy.ComfyError, OSError, ValueError):
                cache[key] = None
        wf_kind = cache[key] or ""
        if wf_kind.endswith("V"):
            kind = "🎬 動画" + (f"  {s.seconds} 秒" if s.seconds else "")
        elif wf_kind:
            kind = "🖼️ 画像"  # 絵文字の形を指定しないと、Windows で四角になることがある
        else:
            kind = "⚠ ワークフローを読めません"
            warn = True
        wf_name = wf_names.get(key)
        if wf_name is None:
            notes.append("ワークフローが登録にありません")
            wf_name = comfy.file_name(s.workflow)
        wf_name = wf_name or comfy.file_name(s.workflow)
        conn_name = conn_names.get(s.url.lower().rstrip("/"))
        if conn_name is None:
            notes.append("接続先が登録にありません")
        conn_label = conn_name or s.url
        machine = conflict(s.url, s.workflow) if conflict else ""
        if machine:
            notes.append(f"ワークフローは {machine} 用です (接続先の台と違います)")
        lines = [f"WF: {wf_name}", f"接続: {conn_label}"]
        steps = []
        if reads:
            steps.append("読取: " + ("—" if s.reader is None else s.reader if s.uses_reader else "使わない"))
        steps.append("盛る: " + ("—" if s.enhancer is None else s.enhancer if s.uses_enhancer else "使わない"))
        lines.append("  ".join(steps))
        used = _insert_names(s)
        if used:
            lines.append("差込: " + "・".join(used))
        if notes:
            warn = True
            if not kind.startswith("⚠"):
                kind += "  ⚠ 台が違う" if machine and len(notes) == 1 else "  ⚠ 登録にない"
        tip = [(s.name or "(名前なし)") + (f"  [{wf_kind}]" if wf_kind else ""),
               f"ワークフロー: {wf_name}  ({s.workflow})", f"接続先: {conn_label}  ({s.url})"]
        if s.seconds:
            tip.append(f"長さ: {s.seconds} 秒")
        def use_text(name: Optional[str], use: bool) -> str:
            if name is None:
                return "(この項目なし。選んでも変えない)"
            if use:
                return name
            return "使わない" + (f" (選んでいるもの: {name})" if name else "")

        if reads:
            tip.append("読み取り: " + use_text(s.reader, s.uses_reader))
            if s.reader is not None and not s.uses_reader and s.prompt:
                tip.append("送るプロンプト (自分で書いたもの): " + (s.prompt if len(s.prompt) <= 200 else s.prompt[:200] + "..."))
            elif s.reread is not None:
                tip.append("読み取り済みでも読み直す: " + ("する" if s.reread else "しない"))
        tip.append("エンハンサー: " + use_text(s.enhancer, s.uses_enhancer))
        if s.inserts is None:
            tip.append("差し込み: (この項目なし。選んでも変えない)")
        else:
            for d in s.inserts:
                if isinstance(d, dict) and d.get("use"):
                    when = "盛る前" if d.get("before") else "盛ったあと"
                    tip.append(f"差し込み「{d.get('name', '')}」: {d.get('folder', '')} ({when})")
            if not used:
                tip.append("差し込み: なし")
        tip.extend(f"⚠ {n}" for n in notes)
        infos.append(SetInfo(s.name or "(名前なし)", kind, lines, "\n".join(tip), warn=warn, wf_kind=wf_kind))
    return infos
