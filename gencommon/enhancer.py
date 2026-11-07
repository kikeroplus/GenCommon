"""プロンプトエンハンサー (AI で盛る)。

プロンプト (本文・説明プロンプト) をローカル LLM に渡して、生成 AI のモデルに合った書き方に書き足してもらう。
モデルごとに推奨される書き方が違うので、指示文 (system)・送る文の雛形 (user)・temperature を
「エンハンサー」のプリセットとして登録し、選んで使う。

プリセット (enhancer_presets.json) と LLM の接続は 3 つのアプリで共有する (%APPDATA%\GenCommon)。
接続先のサーバーは読み取り (と PromptWordCrafter の翻訳) と同じものを使い、LLM のモデルだけ別に選べる。
"""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from . import llm, presets, settings
from .presets import PROMPT_MARK, LlmPreset

PRESETS_FILE = "enhancer_presets.json"  # 共有 (%APPDATA%\GenCommon)
GENERATE_FILE = "enhancer_generate.txt"  # 生成の画面で前回選んだエンハンサーの番号 (0 = なし。アプリごと)
PANEL_FILE = "enhance_preset.txt"  # 画面の「盛る」の欄で選んでいるエンハンサーの番号 (アプリごと)

# 初めて使うときに 1・2 番に入れておく例。モデルごとの推奨の指示文に差し替えて使う
_EXAMPLES = [
    LlmPreset(
        name="例: 自然文 (英語)",
        system=(
            "You are a prompt engineer for text-to-image and text-to-video models.\n"
            "Rewrite the user's prompt into one richly detailed English description in natural sentences: "
            "subject, appearance, action, setting, lighting, camera and style.\n"
            "- Keep every subject and the intent of the original. Do not change who or what is depicted.\n"
            "- The input may be in Japanese or a list of tags; the output must always be English prose.\n"
            "- Keep weighting syntax such as (word:1.2) and special tags such as <lora:...> exactly as written.\n"
            "Output only the rewritten prompt, with no explanation, title or quotation marks."
        ),
        template=PROMPT_MARK,
    ),
    LlmPreset(
        name="例: タグ (カンマ区切り)",
        system=(
            "You are a prompt engineer for anime-style text-to-image models that use comma-separated tags.\n"
            "Expand the user's prompt into a comma-separated list of English tags: keep the original tags first, "
            "then add tags for details, composition, lighting and quality.\n"
            "- Keep every subject and the intent of the original.\n"
            "- The input may be in Japanese; translate it into tags.\n"
            "- Keep weighting syntax such as (word:1.2) and special tags such as <lora:...> exactly as written.\n"
            "Output only the tag list on one line, with no explanation."
        ),
        template=PROMPT_MARK,
    ),
]


def load_presets() -> list[LlmPreset]:
    return presets.load_presets(PRESETS_FILE, _EXAMPLES, PROMPT_MARK)


def last_generate_index() -> int:
    """生成の画面で前回選んだエンハンサー (0 始まり。なしなら -1)。"""
    return presets.load_index(GENERATE_FILE)


def set_last_generate_index(index: int) -> None:
    presets.save_index(GENERATE_FILE, index)


def snapshot(preset: LlmPreset) -> dict:
    """ジョブに持たせる、登録した時点のエンハンサーの写し (接続先とモデルも今のものを入れる)。"""
    return presets.snapshot(preset, settings.llm_url(), settings.enhance_model())


def user_message(template: str, text: str) -> str:
    """雛形の {prompt} を元の文に置き換える。目印がなければ雛形のあとに付ける。"""
    if PROMPT_MARK in template:
        return template.replace(PROMPT_MARK, text)
    return template.rstrip() + "\n\n" + text if template.strip() else text


def enhance(base_url: str, model: str, preset: LlmPreset, text: str) -> str:
    reply = llm.chat(base_url, model, preset.system, user_message(preset.template, text), preset.temperature)
    return presets.clean(reply)


def enhance_with(snap: dict, text: str) -> str:
    """snapshot() の写しで盛る (生成ジョブから使う)。"""
    preset = presets.from_snapshot(snap, PROMPT_MARK)
    return enhance(str(snap.get("url", "")), str(snap.get("model", "")), preset, text)


class EnhancerSettingsDialog(QDialog):
    """LLM のサーバーURL・エンハンサー用のモデル (OK で決まる) と、エンハンサーのプリセット。

    サーバーURL は読み取り・翻訳と同じもの (共有)。
    """

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("エンハンサーの設定")
        self.resize(680, 620)
        layout = QVBoxLayout(self)

        self.status_label = QLabel("サーバーURL は読み取り・翻訳と同じものを使います (3 つのアプリで共有)。")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("hint", True)
        form = QFormLayout()
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(settings.llm_url())
        self.url_edit.setToolTip("LM Studio などの OpenAI 互換 API (例: http://127.0.0.1:1234/v1)")
        url_row.addWidget(self.url_edit, 1)
        test = QPushButton("接続テスト")
        url_row.addWidget(test)
        form.addRow("サーバーURL", url_row)
        self.model = presets.ModelPicker(settings.enhance_model(), self.url, self.status_label.setText)
        test.clicked.connect(self.model.fetch)  # 一覧が取れればつながっている
        form.addRow("LLM のモデル", self.model)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)

        self.editor = presets.PresetEditor(
            "エンハンサーのプリセット (生成 AI のモデルごとに登録します)",
            load_presets(), PRESETS_FILE, PROMPT_MARK,
            "指示文 (system) — モデルごとに推奨されている指示文を貼ります",
            f"送る文の雛形 (user) — {PROMPT_MARK} が盛る前のプロンプトに置き換わります",
            f"ふつうは {PROMPT_MARK} だけで十分です。{PROMPT_MARK} がないときは、雛形のあとにプロンプトを付けて送ります。",
            "高いほど毎回違う盛り方になります (目安 0.5〜1.0)",
            None, index,
        )
        layout.addWidget(self.editor, 1)

        ok_cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_cancel.accepted.connect(self._accept)
        ok_cancel.rejected.connect(self.reject)
        layout.addWidget(ok_cancel)

    def url(self) -> str:
        return self.url_edit.text().strip().rstrip("/")

    def _accept(self):
        if self.url():
            settings.set_llm_url(self.url())
        if self.model.model():
            settings.set_enhance_model(self.model.model())
        self.accept()
