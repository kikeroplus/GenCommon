# GenCommon

PromptWordCrafter と ImageMimicArt (どちらも Python + PySide6) が共通で使う、生成と読み取りの部品です。
J-Rate (C#) は言語が違うので部品は使いませんが、同じ決まりと同じ設定ファイルで動きます。

- 決まりごと: `J-Rate\docs\generation-standard.md` (特に 11 章「3 つのアプリで共有するもの」)
- 置き場所: `C:\Users\ilove\Desktop\WindowsApp\AI_projects\GenCommon\gencommon`。各アプリの `main.py` がこのフォルダを読み込み、EXE を作るときも `.spec` の `pathex` で取り込みます。
- J-Rate・PromptWordCrafter・ImageMimicArt・GenCommon は同じフォルダ (`AI_projects`) に並べて置きます (アプリは隣の `..\GenCommon` を探すので、移すときは 4 つまとめて移します)。

## 中身

| モジュール | 役割 |
|---|---|
| `store.py` | 設定ファイルの置き場所 (共有は `%APPDATA%\GenCommon`、ほかはアプリごと)、初めて共有するときの引き継ぎ、番号の入れ替えを全アプリに伝える |
| `app.py` | アプリごとの違い (`Profile`: 呼び名、出力の置き場所、読み取りの有無、配色) |
| `settings.py` | LLM の接続 (サーバーURL・モデル・送る画像の大きさ。共有) |
| `comfy.py` | プリセット・セット・記録・エクスポート / インポート・ワークフローの書き換え・ComfyUI とのやりとり |
| `generation.py` | 生成ジョブ (同時 2 件、続きから始める、一時停止、読み取り → 盛る → ComfyUI) |
| `comfy_dialogs.py` | 生成の設定・生成する・生成ジョブ・生成時の設定の画面 |
| `bulk_edit.py` | 「まとめて編集...」の画面 (まとめて削除・並べ替え・名前順・空きを詰める) と、結果を一覧に当てはめる `apply` |
| `diagram_dialog.py` | 生成する: 図の画面 (左に設定・真ん中に処理・右に送る前の確認。`docs/relations-columns.html` の並び) |
| `llm.py` / `presets.py` | ローカル LLM の呼び出し、LLM のプリセット (エンハンサー・読み取り) の登録 |
| `enhancer.py` / `reader.py` | エンハンサー (AI で盛る)、読み取り (画像 → 説明プロンプト) とその設定の画面 |

## アプリでの使い方

```python
from gencommon import app
app.setup(app.Profile(name="PromptWordCrafter", reads=False, ...))  # 起動したら最初に
```

アプリ側に残すのは、プロンプトの取り方・出力の置き場所と名前・結果の結び付け・画面など、アプリの役割で違う部分だけです。
