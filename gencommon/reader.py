"""読み取り: 元画像をビジョン対応のローカル LLM に見せて、説明プロンプトを書いてもらう。

頼み方 (指示文・送る文・temperature) は「読み取りプリセット」として登録して選ぶ。
画像は送る前に長い辺を settings.max_side() まで縮め、JPEG にして送る (元より大きくはしない。透過は白で埋める)。
説明プロンプトは、元画像と同じフォルダに拡張子を除いた名前の .txt (photo.png → photo.txt) で置く。
"""

import base64
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import llm, presets, settings
from .presets import LlmPreset

PRESETS_FILE = "reader_presets.json"  # 共有 (%APPDATA%\GenCommon)
GENERATE_FILE = "reader_generate.txt"  # 生成の画面で前回選んだ読み取りプリセットの番号 (アプリごと)
PANEL_FILE = "reader_preset.txt"  # 画面の「読み取る」の欄で選んでいる読み取りプリセットの番号 (アプリごと)
DEFAULT_TEMPLATE = presets.READ_TEMPLATE
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "utf-16", "latin-1")  # PromptWordCrafter と同じ順

# 初めて使うときに 1・2 番に入れておく例。使いながら書き換える
_EXAMPLES = [
    LlmPreset(
        name="例: 自然文の詳しい説明 (英語)",
        system=(
            "You are an expert at writing prompts for text-to-image models.\n"
            "Look at the image and write one detailed English prompt that would recreate it as closely as possible: "
            "the subject (appearance, clothing, pose, expression), setting and background, composition and framing, "
            "lighting, colors, camera and lens feel, and the art style or medium.\n"
            "- Describe only what is visible. Do not invent a story.\n"
            "- Write natural sentences in a single paragraph.\n"
            "Output only the prompt, with no explanation, title or quotation marks."
        ),
        template=DEFAULT_TEMPLATE,
        temperature=0.4,
    ),
    LlmPreset(
        name="例: タグ (カンマ区切り)",
        system=(
            "You are an expert at tagging images for anime-style text-to-image models.\n"
            "Look at the image and list English tags that would recreate it: subject and count, appearance, "
            "clothing, pose, expression, background, composition, lighting and style.\n"
            "- Use short lowercase tags separated by commas, most important first.\n"
            "Output only the tag list on one line, with no explanation."
        ),
        template="List the tags for this image.",
        temperature=0.4,
    ),
]


def load_presets() -> list[LlmPreset]:
    return presets.load_presets(PRESETS_FILE, _EXAMPLES, DEFAULT_TEMPLATE)


def last_generate_index() -> int:
    return presets.load_index(GENERATE_FILE)


def set_last_generate_index(index: int) -> None:
    presets.save_index(GENERATE_FILE, index)


def snapshot(preset: LlmPreset) -> dict:
    """ジョブに持たせる、登録した時点の読み取りプリセットの写し (接続先・モデル・縮める大きさも)。"""
    return presets.snapshot(preset, settings.llm_url(), settings.reader_model(), max_side=settings.max_side())


# ----------------------------------------------------------------------
# 説明プロンプトのファイル
# ----------------------------------------------------------------------
def caption_path(image: Path) -> Path:
    return image.with_suffix(".txt")


def read_caption(image: Path) -> Optional[str]:
    """説明プロンプトの .txt を読む (なければ None)。文字コードは PromptWordCrafter と同じ順で判定する。"""
    try:
        data = caption_path(image).read_bytes()
    except OSError:
        return None
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def write_caption(image: Path, text: str) -> None:
    """UTF-8 で書く。失敗したら OSError。"""
    caption_path(image).write_text(text, encoding="utf-8", newline="")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".jpe", ".webp", ".bmp")


def sharing_note(image: Path) -> str:
    """同じ名前の別の画像 (photo.png と photo.jpg など) と .txt を共用しているときの知らせ (なければ空)。"""
    others = [image.with_suffix(ext).name for ext in IMAGE_EXTS
              if ext != image.suffix.lower() and image.with_suffix(ext).is_file()]
    if not others:
        return ""
    return f" ※ {caption_path(image).name} は {'、'.join(others)} とも共用です"


def captions_to_remove(images: list[Path]) -> list[Path]:
    """画像と一緒にごみ箱へ送る説明プロンプトの .txt (ui-standard 8 章「説明プロンプトの .txt」)。

    同じ名前の別の画像 (photo.png に対する photo.jpg) がフォルダに残るときは、その .txt は残す。
    """
    def key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    going = {key(p) for p in images}
    found: dict[str, Path] = {}
    for image in images:
        txt = caption_path(image)
        if key(txt) in found or not txt.is_file():
            continue
        others = (image.with_suffix(ext) for ext in IMAGE_EXTS if ext != image.suffix.lower())
        if not any(key(other) not in going and other.is_file() for other in others):
            found[key(txt)] = txt
    return list(found.values())


# ----------------------------------------------------------------------
# 読み取る
# ----------------------------------------------------------------------
def image_data_url(path: Path, max_side: int) -> str:
    """画像を長い辺 max_side まで縮めた JPEG にして、data URL で返す。読めなければ LLMError。"""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > max_side:
        reader.setScaledSize(size.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise llm.LLMError(f"画像を読めません: {path.name} ({reader.errorString()})")
    if image.hasAlphaChannel():
        flat = QImage(image.size(), QImage.Format.Format_RGB32)
        flat.fill(QColor("white"))
        painter = QPainter(flat)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flat
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPEG", 90)
    return "data:image/jpeg;base64," + base64.b64encode(bytes(buffer.data())).decode("ascii")


def read_image(base_url: str, model: str, preset: LlmPreset, path: Path, max_side: int) -> str:
    reply = llm.chat(base_url, model, preset.system, preset.template or DEFAULT_TEMPLATE,
                     preset.temperature, image_data_url(path, max_side))
    text = presets.clean(reply)
    if not text:
        raise llm.LLMError("読み取った結果が空でした。")
    return text


def read_with(snap: dict, path: Path) -> str:
    """snapshot() の写しで読み取る (生成ジョブから使う)。"""
    try:
        max_side = int(snap.get("max_side", settings.DEFAULT_MAX_SIDE))
    except (TypeError, ValueError):
        max_side = settings.DEFAULT_MAX_SIDE
    preset = presets.from_snapshot(snap, DEFAULT_TEMPLATE)
    return read_image(str(snap.get("url", "")), str(snap.get("model", "")), preset, path, max_side)


# ----------------------------------------------------------------------
# 設定の窓
# ----------------------------------------------------------------------
class ReaderSettingsDialog(QDialog):
    """LLM のサーバーURL・読み取り用のモデル・縮める大きさ (OK で決まる) と、読み取りプリセット。"""

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("読み取りの設定")
        self.resize(700, 680)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(settings.llm_url())
        self.url_edit.setToolTip("LM Studio などの OpenAI 互換 API (例: http://127.0.0.1:1234/v1)。エンハンサー・翻訳も同じサーバーを使います (3 つのアプリで共有)")
        url_row.addWidget(self.url_edit, 1)
        test = QPushButton("接続テスト")
        url_row.addWidget(test)
        form.addRow("サーバーURL", url_row)
        self.status_label = QLabel("読み取りにはビジョン対応のモデル (qwen2.5-vl、gemma3 など) を選んでください。")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("hint", True)
        self.model = presets.ModelPicker(settings.reader_model(), self.url, self.status_label.setText)
        form.addRow("読み取り用のモデル", self.model)
        side_row = QHBoxLayout()
        self.side_spin = QSpinBox()
        self.side_spin.setRange(256, 4096)
        self.side_spin.setSingleStep(128)
        self.side_spin.setValue(settings.max_side())
        side_row.addWidget(self.side_spin)
        side_row.addWidget(QLabel("px (長い辺をこの大きさまで縮めて送ります。元より大きくはしません)"))
        side_row.addStretch(1)
        form.addRow("送る画像の大きさ", side_row)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)

        self.editor = presets.PresetEditor(
            "読み取りプリセット (画像をどう説明させるか)",
            load_presets(), PRESETS_FILE, DEFAULT_TEMPLATE,
            "指示文 (system) — どんな説明プロンプトを書かせるか",
            "送る文 (user) — 画像と一緒に送る文",
            "画像と一緒に送る短い頼みの文です。空なら既定の文を送ります。",
            "低いほど毎回同じような説明になります (目安 0.2〜0.7)",
            None, index,
        )
        layout.addWidget(self.editor, 1)

        ok_cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_cancel.accepted.connect(self._accept)
        ok_cancel.rejected.connect(self.reject)
        layout.addWidget(ok_cancel)
        test.clicked.connect(self.model.fetch)  # 一覧が取れればつながっている

    def url(self) -> str:
        return self.url_edit.text().strip().rstrip("/")

    def _accept(self):
        if self.url():
            settings.set_llm_url(self.url())
        if self.model.model():
            settings.set_reader_model(self.model.model())
        settings.set_max_side(self.side_spin.value())
        self.accept()
