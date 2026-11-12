"""生成ジョブの管理と、メインウィンドウから呼ぶ「生成する」の流れ。

J-Rate の JRate.cs「生成する (ComfyUI)」以降と同じ動き:
同じ接続先には同時に 2 件まで送り、残りはアプリ側で待機させる。

1 件のジョブは、読み取り (読み取りのあるアプリで、説明プロンプトがまだないとき・読み直すとき) →
AI で盛る (エンハンサーを選んだとき) → ComfyUI の順に進む。差し込み (inserts.py) のファイルは、
登録するときにジョブ 1 件ごとに割り当てておく。同じ元画像の回数ぶんのジョブは、
最初の 1 件だけが読み取り、残りはそれを待って同じ説明を使う。

終わっていないジョブは %APPDATA%/(アプリ名)/comfy_jobs.json に書いておき、
「残して閉じる」で閉じたときや PC が落ちたときは、次に起動したときに続きから始められる。
PromptWordCrafter と ImageMimicArt で共通。アプリごとの違いは app.Profile で切り替える。
"""

import itertools
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

from . import app, comfy, enhancer, inserts, llm, reader
from .comfy_dialogs import ComfySettingsDialog, GenerateDialog, GenInfoDialog, JobWindow
from .diagram_dialog import DiagramGenerateDialog

MAX_RUNNING_JOBS = 2  # 同じ接続先に同時に依頼する件数
CONFIRM_JOB_COUNT = 30  # これを超える件数を登録するときは確かめる
PROGRESS_REFRESH_MS = 300  # 進み具合でジョブ一覧を描き直すのは、この間隔に 1 回まで (ジョブが多くても重くしない)

WAITING, RUNNING, DONE, FAILED, CANCELED = range(5)
STATE_TEXT = {WAITING: "待機中", RUNNING: "実行中", DONE: "完了", FAILED: "失敗", CANCELED: "キャンセル"}

_groups = itertools.count(1)  # 1 回の「生成する」で登録したジョブの組の番号


class GenJob:
    """生成ジョブ 1 件 (元のファイル 1 つを 1 回生成する)。"""

    def __init__(self, source: str, request: comfy.GenRequest, batch: bool, need_read: bool, group: int):
        self.source = source
        self.request = request
        self.batch = batch  # まとめて生成の 1 件 (エラーの窓を出さない、出来ても表示しない)
        self.need_read = need_read  # ComfyUI に送る前に読み取る (読み取ったら False)
        self.group = group  # 同じ組・同じ元画像のジョブは、読み取りを 1 回で済ませる
        self.cancel = threading.Event()
        self.outputs: list[str] = []
        self.state = WAITING
        self.status = ""
        self.prompt_id = ""  # ComfyUI が受け付けた番号 (続きから始めるときに使う)
        self.held = False  # 一時停止中に待機へ戻したもの (再開するまで実行に移さない)
        self.started = 0.0  # 実行に移った時刻 (time.monotonic)
        self.run_time: Optional[float] = None  # 生成にかかった秒数 (完了したときに決まる)
        self.reading = False  # 読み取っている最中 (同じ元画像のほかのジョブを待たせる)

    @property
    def file_name(self) -> str:
        return Path(self.source).name

    @property
    def is_active(self) -> bool:
        return self.state in (WAITING, RUNNING)

    @property
    def is_running(self) -> bool:
        return self.state == RUNNING

    @property
    def state_text(self) -> str:
        return STATE_TEXT[self.state]

    def set_status(self, text: str):
        self.status = " ".join(line for line in (text or "").splitlines() if line)

    def same_image(self, other: "GenJob") -> bool:
        return self.group == other.group and _key(self.source) == _key(other.source)


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class _Signals(QObject):
    """裏のスレッドから、画面のスレッドへ知らせる。"""

    progress = Signal(object, str)
    submitted = Signal(object, str)  # job, prompt_id
    caption_read = Signal(object, str)  # job, 読み取った説明プロンプト
    enhanced = Signal(object)  # job (盛ったプロンプトが決まった)
    finished = Signal(object, object, object)  # job, outputs, error


class GenerationController(QObject):
    """生成ジョブを持ち、各画面を出す。

    prompt_for(path): そのファイルの今のプロンプト (テキストの本文・説明プロンプト。表示中のものは
        画面で編集中の内容。なければ空)。
    show_status(text): ステータスバーに 1 行出す。
    save_caption(path, text): ジョブで読み取った説明プロンプトを .txt に書く (読み取りのあるアプリ。画面のスレッドで呼ぶ)。
    """

    jobs_changed = Signal()
    job_done = Signal(object)  # 生成が終わったジョブ (出来たファイルは job.outputs)

    def __init__(self, window: QWidget, prompt_for: Callable[[Path], str], show_status: Callable[[str], None],
                 save_caption: Optional[Callable[[Path, str], None]] = None):
        super().__init__(window)
        self.window = window
        self.prompt_for = prompt_for
        self.save_caption = save_caption or (lambda path, text: None)
        self.show_status = show_status
        self.jobs: list[GenJob] = []
        self.job_window: Optional[JobWindow] = None
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._signals.submitted.connect(self._on_submitted, Qt.ConnectionType.QueuedConnection)
        self._signals.caption_read.connect(self._on_caption_read, Qt.ConnectionType.QueuedConnection)
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._signals.enhanced.connect(lambda job: self._save_jobs(), Qt.ConnectionType.QueuedConnection)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_job_window)
        self._saved_jobs: Optional[str] = None  # 最後に書いた内容 (変わったときだけ書く)
        # 前回終わらなかったジョブ (offer_resume で聞くまでは書き換えない)
        self._leftover, self._leftover_paused = _load_jobs()
        # 一時停止中は新しく ComfyUI に依頼しない (依頼済みのものは最後まで生成して受け取る)。
        # 終わっていないジョブがなくなったら解く
        self.paused = False

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def active_count(self) -> int:
        return sum(1 for j in self.jobs if j.is_active)

    def job_counts(self) -> str:
        """複数のジョブが残っているときだけ件数を添える。"""
        running = sum(1 for j in self.jobs if j.state == RUNNING)
        waiting = sum(1 for j in self.jobs if j.state == WAITING)
        text = ""
        if running + waiting > 1:
            text = f"  (実行中 {running} 件" + (f"・待機 {waiting} 件" if waiting else "") + ")"
        return text + ("  [一時停止中]" if self.paused else "")

    def set_paused(self, paused: bool):
        if paused == self.paused or (paused and not self.active_count):
            return
        self.paused = paused
        if paused:
            running = sum(1 for j in self.jobs if j.state == RUNNING)
            self.show_status(
                "生成ジョブを一時停止しました。"
                + (f"実行中の {running} 件は最後まで生成します。" if running else "")
            )
        else:
            self.show_status("生成ジョブを再開しました。" + self.job_counts())
        self._start_jobs()

    def confirm_close(self) -> bool:
        """生成中に閉じようとしたら、残すか捨てるかを確かめる。"""
        active = self.active_count
        if not active:
            return True
        if self.paused and not any(j.state == RUNNING for j in self.jobs):
            # 一時停止して止まりきっているなら、聞かずに残す (次に起動したときも一時停止のまま)
            self._save_jobs()
            return True
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("生成中")
        box.setText(f"生成ジョブが {active} 件残っています。")
        if self.paused:
            keep_text = "次に起動したときも一時停止のまま残ります (実行中のものは、起動したときに結果を受け取ります)。"
        else:
            keep_text = "次に起動したときに続きから始められます。"
        box.setInformativeText(
            f"残して閉じる: {keep_text}\n"
            "捨てて閉じる: 残りのジョブは取り消します。\n\n"
            "どちらでも、ComfyUI 側で実行中・順番待ちのものは止まりません。"
        )
        keep = box.addButton("残して閉じる", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("捨てて閉じる", QMessageBox.ButtonRole.DestructiveRole)
        stay = box.addButton("やめる", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(stay)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep:
            self._save_jobs()  # 今の状態を確実に書いておく (裏のスレッドは閉じると止まる)
            return True
        if clicked is discard:
            for job in self.jobs:
                job.cancel.set()
            _write_jobs([])
            return True
        return False

    def offer_resume(self):
        """前回終わらなかったジョブがあれば、続きから始めるか中断するかを聞く。起動の直後に呼ぶ。"""
        leftover, self._leftover = self._leftover, None
        if not leftover:
            return
        jobs = []
        missing = 0
        for item in leftover:
            job = _job_from_dict(item)
            if job is None:
                continue
            if not Path(job.source).is_file():
                missing += 1
                continue
            jobs.append(job)
        if not jobs:
            _write_jobs([])
            if missing:
                QMessageBox.information(
                    self.window, "生成ジョブ",
                    f"前回終わらなかった生成ジョブが {missing} 件ありましたが、"
                    f"{app.profile().source_word}が見つからないため続けられません。",
                )
            return

        if self._leftover_paused:
            # 一時停止したまま閉じたもの: 聞かずに一時停止のまま一覧に戻す
            # (依頼済みのものだけ、ComfyUI から結果を受け取る)
            self.paused = True
            self.jobs.extend(jobs)
            self._start_jobs()
            message = f"前回一時停止した生成ジョブが {len(jobs)} 件あります。ジョブ一覧の「再開」で続きを生成します。"
            if missing:
                message += f" ({app.profile().source_word}が見つからない {missing} 件は除きました)"
            self.show_status(message)
            return

        sent = sum(1 for j in jobs if j.prompt_id)
        detail = f"(ComfyUI に依頼済み {sent} 件・待機 {len(jobs) - sent} 件)"
        if missing:
            detail += f"\n{app.profile().source_word}が見つからない {missing} 件は除きます。"
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("生成ジョブ")
        box.setText(f"前回終わらなかった生成ジョブが {len(jobs)} 件あります。")
        box.setInformativeText(
            detail + "\n\n"
            "続きから始める: 依頼済みのものは ComfyUI から結果を受け取ります "
            "(ComfyUI 側に残っていなければ依頼し直します)。\n"
            "ジョブを中断: 残りのジョブを取り消します。"
        )
        resume = box.addButton("続きから始める", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("ジョブを中断", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(resume)
        box.setEscapeButton(resume)  # うっかり Esc で捨てないように
        box.exec()
        if box.clickedButton() is not resume:
            # ComfyUI に依頼済みのものは、待ち行列から外しておく (つながらなければそのまま)
            def drop():
                for job in jobs:
                    if job.prompt_id:
                        comfy.cancel_prompt(job.request.url, job.prompt_id)

            threading.Thread(target=drop, daemon=True).start()
            _write_jobs([])
            self.show_status(f"前回の生成ジョブ {len(jobs)} 件を中断しました。")
            return
        self.jobs.extend(jobs)
        self.show_jobs()
        self._start_jobs()

    # ------------------------------------------------------------------
    # 生成する
    # ------------------------------------------------------------------
    def _ready(self) -> bool:
        """ワークフロー (と出力フォルダ) が決まっているか。初回は「生成の設定」で決めてもらう。"""
        needs_folder = app.profile().output_folder

        def ok():
            workflow = comfy.active_workflow_path()
            return bool(workflow) and Path(workflow).is_file() and (not needs_folder or bool(comfy.output_folder()))

        if ok():
            return True
        return self.show_settings() and ok()

    def generate(self, sources: list[Path], prefill: Optional[comfy.GenRequest] = None, what: str = ""):
        """設定を聞いてから、sources をジョブに登録する。

        what は、複数のときにダイアログの題に出す対象の呼び名 (「選択中の画像」など)。
        """
        p = app.profile()
        if not sources or not self._ready():
            return

        # プロンプトはダイアログを出す前に読んでおき、画面で見せたものをそのまま送る
        # (表示中のものは、保存前の編集も含めた画面の内容)。読み取りのあるアプリでは、空ならジョブの中で読み取る
        captions = [(source, self.prompt_for(source).strip()) for source in sources]
        skipped = 0
        if p.skip_empty and len(captions) > 1:
            kept = [(source, text) for source, text in captions if text]
            skipped = len(captions) - len(kept)
            captions = kept
            if not captions:
                QMessageBox.information(self.window, "生成する", f"{p.prompt_word}のある{p.source_word}がありません。")
                return
        if len(captions) > 1:
            title = f"まとめて生成する - {what} {len(captions)} {p.unit}"
            if skipped:
                title += f" ({p.prompt_word}が空の {skipped} {p.unit}は除きます)"
        else:
            title = f"生成する - {captions[0][0].name}"

        diagram = True  # 図の画面で始める (「いつもの画面で開く」で切り替える)
        carry = None
        while True:
            names = [(source.name, text) for source, text in captions]
            if diagram:
                dialog = DiagramGenerateDialog(title, names, prefill, self.window, carry,
                                               sources=[source for source, _text in captions],
                                               save_caption=self.save_caption)
            else:
                dialog = GenerateDialog(title, names, prefill, self.window, carry)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            # 図の画面の「今読み取る」で読み取ったものは、そのまま使う (ジョブで読み直さない)
            captions = [(source, text) for (source, _old), (_name, text) in zip(captions, dialog.captions)]
            if accepted:
                break
            if dialog.reopen is None:
                return
            carry = None
            if dialog.switch_view:
                # いつもの画面 ⇔ 図の画面: 入力した内容のまま、もう一方で開き直す
                diagram = not diagram
                prefill = dialog.reopen
                carry = dialog.carry
                continue
            # 「生成の設定...」が押された: 設定を出してから、入力していた内容で開き直す。
            # 設定を OK で閉じたときは、そこで選んだ接続先とワークフローを使う
            prefill = dialog.reopen
            if self.show_settings():
                prefill.url = ""
                prefill.workflow_path = ""
        request = dialog.request
        repeat = dialog.repeat
        if p.output_folder:
            request.output_dir = comfy.output_folder()
        if dialog.enhancer_preset is not None:
            # 中身は登録した時点の写しで盛る (あとでエンハンサーの設定を変えても、頼んだときの内容で)
            request.enhancer = enhancer.snapshot(dialog.enhancer_preset)
        if p.reads and dialog.reader_preset is not None:
            request.reader = reader.snapshot(dialog.reader_preset)

        total = len(captions) * repeat
        if total > CONFIRM_JOB_COUNT and QMessageBox.question(
            self.window, title, f"合計 {total} 件の生成を登録します。よろしいですか？",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Ok:
            return

        # 差し込み: ジョブ 1 件ごとに、使う枠の次のファイルを割り当てる (全部割り当ててから位置を書く)
        try:
            picker = inserts.Picker(dialog.insert_slots)
        except inserts.InsertError as exc:
            QMessageBox.warning(self.window, title, f"差し込みのフォルダを使えないので、生成しませんでした。\n\n{exc}")
            return
        new_jobs = []
        to_read = 0
        try:
            batch = total > 1
            group = next(_groups)
            for source, text in captions:
                need_read = p.reads and (dialog.reread or not text)
                to_read += need_read
                for _ in range(repeat):
                    req = request.copy(prompt=None if need_read else text, inserts=picker.next())
                    new_jobs.append(GenJob(str(source), req, batch, need_read, group))
        except inserts.InsertError as exc:
            picker.discard()
            QMessageBox.warning(self.window, title, f"差し込みのフォルダを使えないので、生成しませんでした。\n\n{exc}")
            return
        picker.commit()
        self.jobs.extend(new_jobs)
        if batch or self.paused:
            self.show_jobs()
        self._start_jobs()
        message = f"{to_read} {p.unit}は、ジョブの中で読み取ってから生成します。" if to_read else ""
        if skipped:
            message += f"{p.prompt_word}が空の {skipped} {p.unit}は生成しませんでした。"
        if self.paused:
            message += "一時停止中です。ジョブ一覧の「再開」で生成を始めます。"
        if message:
            self.show_status(message + self.job_counts())

    def _start_jobs(self):
        """待機中のジョブを、接続先ごとに MAX_RUNNING_JOBS 件まで実行に移す。"""
        for job in [j for j in self.jobs if j.state == WAITING]:
            if self.paused and (not job.prompt_id or job.held):
                continue  # 一時停止中は新しく依頼しない (依頼済みのものは結果を受け取る)
            if job.need_read and any(j.reading and j.same_image(job) for j in self.jobs):
                continue  # 同じ元画像を読み取っている最中: その結果を待つ
            url = job.request.url
            if sum(1 for j in self.jobs if j.state == RUNNING and j.request.url == url) >= MAX_RUNNING_JOBS:
                continue
            self._run(job)
        self._changed()

    def _run(self, job: GenJob):
        job.state = RUNNING
        job.held = False
        job.reading = job.need_read
        job.started = time.monotonic()
        job.set_status("準備中...")
        self._show_job_status(job)
        signals = self._signals
        prompt_id = job.prompt_id

        def work():
            outputs = None
            error = None
            try:
                if job.need_read and not prompt_id:
                    self._read(job, signals)
                req = job.request
                if req.enhancer is not None and req.enhanced_prompt is None and (req.source_prompt or "").strip():
                    self._enhance(job, signals)
                outputs = comfy.generate(
                    job.source, job.request, lambda text: signals.progress.emit(job, text), job.cancel,
                    prompt_id, lambda new_id: signals.submitted.emit(job, new_id),
                    lambda seconds: setattr(job, "run_time", seconds),
                    lambda: self.paused,
                )
                for path in outputs:
                    comfy.add_history(path, job.source, job.request)  # どの設定で作ったかを残す
            except Exception as exc:  # noqa: BLE001 - どんな失敗もジョブの失敗として見せる
                error = exc
            signals.finished.emit(job, outputs, error)

        threading.Thread(target=work, daemon=True).start()

    def _wait_llm(self, job: GenJob, func: Callable[[], str], failed: str, empty: str) -> str:
        """LLM の返事を待つ (裏のスレッドで呼ぶ)。待つ間もキャンセルできる。失敗したら ComfyError。

        failed は失敗したとき、empty は返事が空だったときの文。
        """
        box: dict = {}

        def call():
            try:
                box["text"] = func()
            except Exception as exc:  # noqa: BLE001 - 下でジョブの失敗にする
                box["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        while worker.is_alive():
            if job.cancel.is_set():
                raise comfy.GenerationCancelled()
            worker.join(0.2)
        if job.cancel.is_set():
            raise comfy.GenerationCancelled()
        error = box.get("error")
        if isinstance(error, llm.LLMError):
            raise comfy.ComfyError(f"{failed}{error}")
        if error is not None:
            raise comfy.ComfyError(f"{failed}{type(error).__name__}: {error}")
        text = (box.get("text") or "").strip()
        if not text:
            raise comfy.ComfyError(empty)
        return text

    def _read(self, job: GenJob, signals: "_Signals"):
        """ComfyUI に送る前に、元画像を読み取って説明プロンプトを作る (裏のスレッドで呼ぶ)。

        読み取れなければジョブの失敗にする (説明なしでは送らない)。読み取った説明はジョブに残し、
        .txt にも書く (同じ元画像のほかのジョブにも渡す)。
        """
        if job.request.reader is None:
            raise comfy.ComfyError("読み取りプリセットが選ばれていないので、読み取れません。")
        signals.progress.emit(job, "読み取っています...")
        text = self._wait_llm(job, lambda: reader.read_with(job.request.reader, Path(job.source)),
                              "読み取れませんでした。", "読み取った結果が空でした。")
        job.request = job.request.copy(prompt=text)
        job.need_read = False
        signals.caption_read.emit(job, text)  # job.reading は画面のスレッドで下ろす (待っているジョブに渡してから)
        if self.paused:
            raise comfy.GenerationHeld(sent=False)  # 読み取った説明を持ったまま待機に戻り、再開を待つ

    def _enhance(self, job: GenJob, signals: "_Signals"):
        """ComfyUI に送る前に、説明プロンプトを AI で盛る (裏のスレッドで呼ぶ)。

        ジョブごとに盛るので、回数を増やすと毎回違う盛り方になる。盛ったプロンプトはジョブに残し、
        続きから始めるときはもう盛らない。盛れなければジョブの失敗にする (元のプロンプトのまま送らない)。
        """
        signals.progress.emit(job, "AI で盛っています...")
        text = self._wait_llm(job, lambda: enhancer.enhance_with(job.request.enhancer, job.request.source_prompt),
                              "AI で盛れませんでした。", "盛ったプロンプトが空でした。")
        job.request = job.request.copy(enhanced_prompt=text)  # 回数ぶんのジョブで写しを共有しないよう、作り直す
        signals.enhanced.emit(job)
        if self.paused:
            raise comfy.GenerationHeld(sent=False)  # 盛ったプロンプトを持ったまま待機に戻り、再開を待つ

    def _on_caption_read(self, job: GenJob, text: str):
        """読み取った説明を .txt に書き、同じ元画像を待っているジョブにも渡す。"""
        job.reading = False
        self.save_caption(Path(job.source), text)
        for other in self.jobs:
            if other is not job and other.need_read and other.state == WAITING and other.same_image(job):
                other.request = other.request.copy(prompt=text)
                other.need_read = False
        self._save_jobs()
        self._start_jobs()

    def _on_progress(self, job: GenJob, text: str):
        if job.state != RUNNING:
            return  # 完了後に届いた古い進捗は無視
        job.set_status("キャンセル中..." if job.cancel.is_set() else text)
        self._show_job_status(job)
        # 進み具合は件数も保存する内容も変えないので、ジョブ一覧を描き直すだけ (間引いて)
        if self.job_window is not None and not self._refresh_timer.isActive():
            self._refresh_timer.start(PROGRESS_REFRESH_MS)

    def _refresh_job_window(self):
        if self.job_window is not None:
            self.job_window.refresh()

    def _on_submitted(self, job: GenJob, prompt_id: str):
        job.prompt_id = prompt_id
        self._save_jobs()

    def _on_finished(self, job: GenJob, outputs, error):
        failed = False
        message = ""
        job.reading = False
        if isinstance(error, comfy.GenerationHeld) and not job.cancel.is_set():
            # 一時停止中に ComfyUI 側から消えた・つながらなくなった: 待機に戻して再開を待つ
            job.state = WAITING
            job.held = True
            if not error.sent:
                job.prompt_id = ""
            job.set_status("一時停止のため待機に戻しました")
            self.show_status(f"{job.file_name} を待機に戻しました" + self.job_counts())
        elif isinstance(error, comfy.GenerationCancelled) or (error is not None and job.cancel.is_set()):
            job.state = CANCELED
            job.set_status("キャンセルしました")
            self.show_status(f"生成をキャンセルしました: {job.file_name}" + self.job_counts())
        elif error is not None:
            failed = True
            job.state = FAILED
            if isinstance(error, comfy.ComfyConnectionError):
                message = (
                    f"ComfyUI に接続できません ({job.request.url})。\n"
                    f"ComfyUI が起動しているか確認してください。\n\n{error}"
                )
            elif isinstance(error, comfy.ComfyError):
                message = str(error)
            else:
                message = f"生成できませんでした。\n{error}"
            job.set_status(message)
            self.show_status(f"生成できませんでした: {job.file_name}" + self.job_counts())
        else:
            job.outputs = list(outputs)
            job.state = DONE
            if job.run_time is None:
                # ComfyUI の記録から分からなかったときは、アプリで実行に移してからの時間
                job.run_time = time.monotonic() - job.started
            extra = f" ほか {len(outputs) - 1} 件" if len(outputs) > 1 else ""
            job.set_status(f"生成しました: {Path(outputs[0]).name}{extra}")
            self.show_status(job.status + self.job_counts())
            # 1 件だけの生成なら、出来たファイルを開く (開くかはアプリで違う。並べて見せるアプリは job_done で受ける)
            if app.profile().open_on_done and not job.batch and comfy.open_result():
                self._open_file(outputs[0])
            self.job_done.emit(job)

        self._start_jobs()
        if not any(j.is_active for j in self.jobs):
            self._notify_finished()
        if failed and not job.batch:
            QMessageBox.warning(self.window, "生成する", f"{job.file_name}\n\n{message}")

    def cancel_job(self, job: GenJob):
        """待機中ならその場で取り消し、実行中なら ComfyUI 側の実行も止める。"""
        if job.state == WAITING:
            job.state = CANCELED
            job.set_status("キャンセルしました")
            self._changed()
        elif job.state == RUNNING and not job.cancel.is_set():
            job.set_status("キャンセル中...")
            job.cancel.set()
            self._changed()

    def _show_job_status(self, job: GenJob):
        self.show_status(f"生成 {job.file_name} : {job.status}" + self.job_counts())

    def _changed(self):
        if self.paused and not self.active_count:
            self.paused = False
        if self.job_window is not None:
            self.job_window.refresh()
        self.jobs_changed.emit()
        self._save_jobs()

    def _save_jobs(self):
        """終わっていないジョブを書いておく。前回のジョブをまだ聞いていないうちは書かない。"""
        if self._leftover is not None:
            return
        items = [_job_to_dict(j) for j in self.jobs if j.is_active]
        text = json.dumps([self.paused, items], ensure_ascii=False)
        if text == self._saved_jobs:
            return
        if _write_jobs(items, self.paused):
            self._saved_jobs = text

    def _notify_finished(self):
        """すべてのジョブが終わった。ほかの作業をしているときはタスクバーの点滅と音で知らせる。"""
        if QApplication.activeWindow() is not None:
            return
        QApplication.alert(self.window, 0)
        QApplication.beep()

    def _open_file(self, path: str):
        try:
            os.startfile(path)  # 関連付けられたアプリで開く
        except OSError as exc:
            self.show_status(f"ファイルを開けません: {Path(path).name} ({exc})")

    # ------------------------------------------------------------------
    # 各画面
    # ------------------------------------------------------------------
    def show_jobs(self):
        if self.job_window is not None:
            self.job_window.show()
            self.job_window.raise_()
            self.job_window.activateWindow()
            return
        self.job_window = JobWindow(self, self.cancel_job, self._show_result, self.window)
        self.job_window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.job_window.destroyed.connect(self._job_window_closed)
        self.job_window.show()

    def _job_window_closed(self):
        self.job_window = None

    def _show_result(self, job: GenJob):
        path = next((p for p in job.outputs if Path(p).is_file()), None)
        if path is None:
            self.show_status(f"ファイルが見つかりません: {Path(job.outputs[0]).name}")
            return
        self._open_file(path)

    def show_settings(self) -> bool:
        # インポートしたあとは、読み込んだ内容で開き直す
        while True:
            dialog = ComfySettingsDialog(self.window)
            ok = dialog.exec() == QDialog.DialogCode.Accepted
            if not dialog.imported:
                return ok

    def show_diagram_settings(self, sources: Optional[list[Path]] = None):
        """「図で設定」: 図の画面で、次に生成するときの設定 (前回の値) を決めて保存する。生成はしない。

        sources があれば、1 件目の本文 (説明プロンプト) で送る前の確認を見せる。
        """
        captions = [(source.name, self.prompt_for(source).strip()) for source in (sources or [])[:1]]
        if not captions:
            captions = [("", "")]  # ファイルを選んでいない (図の画面は〈本文〉で示す)
        prefill = None
        while True:
            dialog = DiagramGenerateDialog("図で設定", captions, prefill, self.window, settings_only=True,
                                           sources=(sources or [])[:1], save_caption=self.save_caption)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.show_status("生成の設定を保存しました。次に「生成する」で開いたときに、この設定で始まります。")
                return
            if dialog.reopen is None:
                return
            # 「生成の設定...」が押された: 設定を出してから、入力していた内容で開き直す
            prefill = dialog.reopen
            if self.show_settings():
                prefill.url = ""
                prefill.workflow_path = ""

    def has_history(self, path: Path) -> bool:
        return comfy.find_history_by_source(str(path)) is not None

    def show_gen_info(self, path: Path):
        """そのファイルから最後に生成したときの設定を見せる。"""
        record = comfy.find_history_by_source(str(path))
        if record is None:
            QMessageBox.information(
                self.window, "生成時の設定", f"{path.name}\n\nこの{app.profile().source_word}から生成した記録はありません。"
            )
            return
        self.show_record(record, path.name)

    def show_record(self, record: comfy.GenRecord, name: str = ""):
        """記録 1 件の中身を見せる (生成結果のサムネールからは、そのファイルを作ったときの記録)。"""
        source = Path(record.source)
        current = self.prompt_for(source) if source.is_file() else None
        dialog = GenInfoDialog(name or Path(record.output).name, record, current, self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.regenerate:
            # 接続先・ワークフロー・長さ・読み取りプリセット・エンハンサーは記録のまま、プロンプトは今のもの
            self.generate([source], record.request)


# ----------------------------------------------------------------------
# 終わっていないジョブ (%APPDATA%\(アプリ名)\comfy_jobs.json)
# ----------------------------------------------------------------------
JOBS_FILE = "comfy_jobs.json"


def _job_to_dict(job: GenJob) -> dict:
    return {
        "source": job.source,
        "batch": job.batch,
        "promptId": job.prompt_id,
        "needRead": job.need_read,
        "group": job.group,
        "request": dict(job.request.__dict__),
    }


def _job_from_dict(data) -> Optional[GenJob]:
    if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
        return None
    source = data.get("source")
    if not isinstance(source, str) or not source:
        return None
    fields = comfy.GenRequest.__dataclass_fields__
    try:
        request = comfy.GenRequest(**{k: v for k, v in data["request"].items() if k in fields})
    except TypeError:
        return None
    need_read = app.profile().reads and (bool(data.get("needRead")) or request.prompt is None)
    # 前回の組の番号は、今回の番号とぶつからないよう負の数にしておく
    try:
        group = -abs(int(data.get("group") or 0))
    except (TypeError, ValueError):
        group = 0
    job = GenJob(source, request, bool(data.get("batch")), need_read, group)
    job.prompt_id = str(data.get("promptId") or "")
    return job


def _load_jobs() -> tuple[list, bool]:
    """(ジョブの一覧, 一時停止したまま閉じたか)。"""
    try:
        path = comfy.file_path(JOBS_FILE)
        if not path.is_file():
            return [], False
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return [], False
    if isinstance(data, dict):
        jobs = data.get("jobs")
        return (jobs if isinstance(jobs, list) else []), bool(data.get("paused"))
    return (data if isinstance(data, list) else []), False


def _write_jobs(items: list, paused: bool = False) -> bool:
    """書けたら True。空なら消す。書きかけで落ちても壊れないよう、別名に書いてから置き換える。"""
    try:
        path = comfy.file_path(JOBS_FILE)
        if not items:
            if path.exists():
                path.unlink()
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        data = {"paused": paused, "jobs": items}
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)
        return True
    except OSError:
        return False
