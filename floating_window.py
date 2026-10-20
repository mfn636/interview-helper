"""可置顶的语音面试悬浮窗，使用 Python 自带的 Tkinter。"""

import tkinter as tk
import sys
from queue import Empty, SimpleQueue
from threading import Thread
from tkinter import ttk
from tkinter.font import Font

from deepseek_client import ROLE_LABELS
from floating_session import ChatSession
from project_order_dialog import ProjectOrderDialog
from speech_session import SpeechSession
from system_audio import list_output_devices
from voice_controller import VoiceController


class FloatingWindow:
    BACKGROUND = "#080808"
    SURFACE = "#141414"
    INK = "#eeeeee"
    MUTED = "#aaaaaa"
    ACCENT = "#dedede"
    BORDER = "#363636"

    def __init__(self, root: tk.Tk, session: ChatSession | None = None,
                 speech: SpeechSession | None = None):
        self.root = root
        self.session = session if session is not None else ChatSession()
        self.speech = speech if speech is not None else SpeechSession()
        self.voice = VoiceController(self.session)
        self.voice.pause()
        self.device_results = SimpleQueue()
        self.device_request = 0
        self.devices = {}
        self.closed = False
        self.poll_timer = None
        self.copy_timer = None
        self.last_answer = ""
        self.waiting_ticks = 0
        self.ready_message = ""
        self.project_dialog = None
        self.topmost = tk.BooleanVar(value=True)
        self.opacity = tk.IntVar(value=100)
        self.status = tk.StringVar(value="正在初始化…")
        self.turn_count = tk.StringVar(value="0 轮对话")
        self.audio_device = tk.StringVar(value="Windows 默认输出")
        self.speech_state = tk.StringVar(value="未开始监听")
        self.queue_status = tk.StringVar(value="停顿后自动回答")

        root.title("面试助手 · 悬浮窗")
        root.configure(bg=self.BACKGROUND)
        root.geometry(f"560x840+{max(20, root.winfo_screenwidth() - 610)}+40")
        root.minsize(460, 680)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._dark_titlebar()
        self.refresh_devices()
        self.initialize()
        self._poll()

    def _button(self, parent, text, command, *, primary=False):
        return tk.Button(
            parent, text=text, command=command, font=("Microsoft YaHei UI", 10),
            bg="#e2e2e2" if primary else "#262626",
            fg="#161616" if primary else self.INK,
            activebackground="#ffffff" if primary else "#3a3a3a",
            activeforeground="#101010" if primary else self.INK,
            disabledforeground="#777777", relief="flat", borderwidth=0,
            padx=15, pady=8, cursor="hand2", takefocus=True,
        )

    def _dark_titlebar(self):
        """Windows 支持时让原生标题栏与深色界面一致。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            handle = user32.GetParent(self.root.winfo_id())
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            enabled = ctypes.c_int(1)
            if set_attribute(handle, 20, ctypes.byref(enabled), ctypes.sizeof(enabled)) != 0:
                set_attribute(handle, 19, ctypes.byref(enabled), ctypes.sizeof(enabled))
        except (AttributeError, OSError):
            pass

    def _build(self):
        self.body_font = Font(family="Microsoft YaHei UI", size=11)
        self.small_font = Font(family="Microsoft YaHei UI", size=9)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Helper.Vertical.TScrollbar", background="#484848", troughcolor=self.SURFACE,
                        bordercolor=self.SURFACE, arrowcolor=self.MUTED, width=10)
        style.map("Helper.Vertical.TScrollbar", background=[("active", "#686868")])
        style.configure("Helper.TCombobox", fieldbackground=self.SURFACE, background="#262626",
                        foreground=self.INK, arrowcolor=self.INK, bordercolor=self.BORDER)
        style.map("Helper.TCombobox", fieldbackground=[("readonly", self.SURFACE)],
                  foreground=[("readonly", self.INK), ("disabled", "#777777")])
        self.root.option_add("*TCombobox*Listbox.background", self.SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", self.INK)

        self.body = tk.Frame(self.root, bg=self.BACKGROUND)
        self.body.pack(fill="both", expand=True)
        toolbar = tk.Frame(self.body, bg=self.BACKGROUND, padx=18)
        toolbar.pack(fill="x", pady=(10, 13))
        tk.Checkbutton(toolbar, text="保持置顶", variable=self.topmost, command=self._set_topmost,
                       bg=self.BACKGROUND, activebackground=self.BACKGROUND, fg=self.INK,
                       activeforeground=self.INK,
                       selectcolor=self.SURFACE, font=self.small_font, borderwidth=0).pack(side="left")
        self.opacity_label = tk.Label(toolbar, text="不透明度 100%", bg=self.BACKGROUND,
                                      fg=self.MUTED, font=self.small_font)
        self.opacity_label.pack(side="right")
        tk.Scale(toolbar, from_=65, to=100, variable=self.opacity, orient="horizontal", showvalue=False,
                 command=self._set_opacity, length=90, width=5, sliderlength=13, highlightthickness=0,
                 borderwidth=0, bg="#727272", troughcolor="#363636",
                 activebackground=self.ACCENT).pack(side="right", padx=(8, 10))

        conversation = tk.Frame(self.body, bg=self.SURFACE, highlightbackground=self.BORDER,
                                highlightthickness=1)
        conversation.pack(fill="both", expand=True, padx=18)
        conversation_header = tk.Frame(conversation, bg=self.SURFACE, padx=13, pady=11)
        conversation_header.pack(fill="x")
        self.conversation_title = tk.Label(conversation_header, text="回答与对话", bg=self.SURFACE,
                                           fg=self.INK, font=("Microsoft YaHei UI", 10, "bold"))
        self.conversation_title.pack(side="left")
        tk.Label(conversation_header, textvariable=self.turn_count, bg=self.SURFACE, fg=self.MUTED,
                 font=self.small_font).pack(side="right")
        self.role_button = self._button(conversation_header, "", self.switch_role)
        self.role_button.configure(font=self.small_font, pady=4, padx=8)
        self.role_button.pack(side="right", padx=(8, 12))
        transcript_frame = tk.Frame(conversation, bg=self.SURFACE)
        transcript_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(transcript_frame, orient="vertical", style="Helper.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        self.transcript = tk.Text(
            transcript_frame, wrap="word", font=self.body_font, bg=self.SURFACE, fg=self.INK,
            borderwidth=0, highlightthickness=0, padx=14, pady=8, height=8,
            spacing1=4, spacing3=8, state="disabled", cursor="arrow",
            selectbackground="#484848", selectforeground="white", yscrollcommand=scrollbar.set,
        )
        self.transcript.pack(fill="both", expand=True)
        scrollbar.configure(command=self.transcript.yview)
        self.transcript.tag_configure("label", foreground=self.ACCENT, font=("Microsoft YaHei UI", 9, "bold"))
        self.transcript.tag_configure("question", foreground="#bbbbbb")
        self.transcript.tag_configure("hint", foreground=self.MUTED, font=self.small_font)
        self.transcript.tag_configure("error", foreground="#cccccc")

        actions = tk.Frame(self.body, bg=self.BACKGROUND, padx=18, pady=9)
        actions.pack(fill="x")
        self.copy_button = self._button(actions, "复制最新回答", self.copy_answer)
        self.copy_button.pack(side="left")
        self.copy_button.configure(state="disabled", font=self.small_font, pady=4, padx=10)
        self.clear_button = self._button(actions, "新对话", self.clear)
        self.clear_button.configure(font=self.small_font, pady=4, padx=10)
        self.clear_button.pack(side="right")

        audio_frame = tk.Frame(self.body, bg=self.SURFACE, highlightbackground=self.BORDER,
                               highlightthickness=1, padx=11, pady=9)
        audio_frame.pack(fill="x", padx=18)
        device_row = tk.Frame(audio_frame, bg=self.SURFACE)
        device_row.pack(fill="x")
        tk.Label(device_row, text="系统声音", bg=self.SURFACE, fg=self.MUTED,
                 font=self.small_font).pack(side="left", padx=(0, 8))
        self.refresh_button = self._button(device_row, "刷新", self.refresh_devices)
        self.refresh_button.configure(font=self.small_font, pady=3, padx=8)
        self.refresh_button.pack(side="right", padx=(6, 0))
        self.device_selector = ttk.Combobox(device_row, textvariable=self.audio_device,
                                           values=("Windows 默认输出",), state="readonly",
                                           style="Helper.TCombobox", font=self.small_font)
        self.device_selector.pack(side="left", fill="x", expand=True)
        preview_header = tk.Frame(audio_frame, bg=self.SURFACE)
        preview_header.pack(fill="x", pady=(10, 0))
        tk.Label(preview_header, text="实时转写", bg=self.SURFACE, fg=self.MUTED,
                 font=self.small_font).pack(side="left")
        tk.Label(preview_header, textvariable=self.speech_state, bg=self.SURFACE, fg=self.MUTED,
                 font=self.small_font).pack(side="right")
        self.preview = tk.Text(audio_frame, height=3, wrap="word", font=self.body_font,
                               bg=self.SURFACE, fg=self.INK, borderwidth=0, highlightthickness=0,
                               pady=8, state="disabled", cursor="arrow", selectbackground="#484848")
        self.preview.pack(fill="x")
        speech_actions = tk.Frame(audio_frame, bg=self.SURFACE)
        speech_actions.pack(fill="x")
        self.listen_button = self._button(speech_actions, "开始监听", self.toggle_listening, primary=True)
        self.listen_button.pack(side="left")
        self.skip_button = self._button(speech_actions, "跳过失败问题", self.skip_failed)
        self.skip_button.configure(font=self.small_font, padx=8)
        self.skip_button.pack(side="right", padx=(5, 0))
        self.retry_button = self._button(speech_actions, "重试", self.retry)
        self.retry_button.configure(font=self.small_font, padx=8)
        self.retry_button.pack(side="right")
        tk.Label(audio_frame, textvariable=self.queue_status, bg=self.SURFACE, fg=self.MUTED,
                 font=self.small_font, anchor="w").pack(fill="x", pady=(8, 0))
        self.status_label = tk.Label(self.body, textvariable=self.status, bg=self.BACKGROUND,
                                    fg=self.MUTED, font=("Microsoft YaHei UI", 8), anchor="w", justify="left")
        self.status_label.pack(fill="x", padx=20, pady=(9, 13))
        self.body.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(300, event.width - 40)))

    def initialize(self):
        if self.session.busy:
            return
        self.waiting_ticks = 0
        self.clear_button.configure(state="disabled")
        self.listen_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.skip_button.configure(state="disabled")
        self.status.set("正在确认姓名与岗位…" if self.session.project_order_confirmed else
                        "正在加载提示词、简历和岗位 JD，并从简历识别项目…")
        self.session.initialize()
        self._update_controls()

    def switch_role(self):
        if self.session.busy:
            self.status.set("请等待当前回复完成后再切换角色。")
            return
        role = "interviewer" if self.session.role == "candidate" else "candidate"
        try:
            self.session.set_role(role)
        except Exception as exc:
            self.status.set(f"切换角色失败：{exc}")
            return
        if self.session.ready:
            self.clear()
            action = "点击开始监听，转写内容将作为候选人的发言" if role == "interviewer" else (
                "点击开始监听，转写内容将作为面试官的提问")
        else:
            action = "请先完成面试资料与项目顺序确认"
        self.status.set(f"已切换为{ROLE_LABELS[role]} · {action}")
        self._update_controls()

    def _show_ready(self, text):
        self._append("面试助手\n", "label")
        self._append(text + "\n")
        if self.session.project_order:
            self._append("项目熟悉度：" + " → ".join(self.session.project_order) + "\n", "hint")

    def _show_project_selection(self):
        if not self.session.awaiting_project_order:
            return
        if self.project_dialog is not None:
            self.project_dialog.show()
            return
        self.project_dialog = ProjectOrderDialog(
            self.root, self.session.projects, self._confirm_projects, self.initialize,
            self._project_dialog_closed,
        )

    def _project_dialog_closed(self):
        self.project_dialog = None

    def _confirm_projects(self, order):
        self.session.confirm_projects(order)
        self.waiting_ticks = 0
        self.status.set("项目顺序已确认，正在确认姓名与岗位…")
        self._update_controls()

    def _append(self, text, tag=None):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, (tag,) if tag else ())
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _set_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def _set_opacity(self, value):
        percentage = max(65, min(100, int(float(value))))
        self.root.attributes("-alpha", percentage / 100)
        self.opacity_label.configure(text=f"不透明度 {percentage}%")

    def refresh_devices(self):
        if self.speech.running:
            return
        self.device_request += 1
        request = self.device_request
        self.refresh_button.configure(state="disabled")

        def load():
            try:
                self.device_results.put((request, list_output_devices(), None))
            except Exception as exc:
                self.device_results.put((request, [], str(exc)))

        Thread(target=load, daemon=True, name="list-audio-devices").start()

    def _poll_devices(self):
        while True:
            try:
                request, devices, error = self.device_results.get_nowait()
            except Empty:
                return
            if request != self.device_request:
                continue
            self.refresh_button.configure(state="normal" if not self.speech.running else "disabled")
            self.devices = {f"{item['index']} · {item['name']}": item["index"] for item in devices}
            values = ["Windows 默认输出", *self.devices]
            self.device_selector.configure(values=values)
            if self.audio_device.get() not in values:
                self.audio_device.set(values[0])
            if error:
                self.status.set(error)

    def _set_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("end", text)
        self.preview.configure(state="disabled")
        self.preview.see("end")

    def _update_controls(self):
        listening = self.speech.running
        interviewer = self.session.role == "interviewer"
        role = ROLE_LABELS[self.session.role]
        target = ROLE_LABELS["candidate" if interviewer else "interviewer"]
        self.role_button.configure(text=f"{role} → 切换为{target}",
                                   state="disabled" if self.session.busy else "normal")
        self.conversation_title.configure(text="提问与对话" if interviewer else "回答与对话")
        if self.copy_timer is None:
            self.copy_button.configure(text=self._copy_label())
        self.clear_button.configure(text="新对话" if self.session.ready else "重新加载资料",
                                    state="normal" if self.session.ready or not self.session.busy else "disabled")
        self.listen_button.configure(text="暂停监听" if listening else "开始监听",
                                     state="normal" if self.session.ready else "disabled")
        self.device_selector.configure(state="disabled" if listening else "readonly")
        self.refresh_button.configure(state="disabled" if listening else "normal")
        failed = self.voice.failed_prompt is not None
        self.retry_button.configure(text=("重试提问" if interviewer else "重试回答") if self.session.ready else (
                                        "确认项目顺序" if self.session.awaiting_project_order else "重试初始化"),
                                    state="normal" if failed or (not self.session.ready and not self.session.busy)
                                    else "disabled")
        self.skip_button.configure(text="跳过失败发言" if interviewer else "跳过失败问题",
                                   state="normal" if failed else "disabled")

    def toggle_listening(self):
        if self.speech.running:
            self.speech.stop()
            self.voice.pause()
            self._set_preview("")
            self.speech_state.set("已暂停")
            self.status.set("已暂停采集和自动提交，完整待答问题保留。")
        elif self.session.ready:
            try:
                self.speech.start(self.devices.get(self.audio_device.get()))
            except Exception as exc:
                self.status.set(str(exc))
                return
            self.voice.resume()
            self.speech_state.set("正在启动…")
            self.status.set("正在启动系统声音监听。")
        self._update_controls()

    def retry(self):
        if self.session.awaiting_project_order:
            self._show_project_selection()
        elif not self.session.ready:
            self.initialize()
        else:
            self.voice.retry()
            self.status.set("问题已重新排队。" if self.speech.running else "问题已重新排队，开始监听后继续回答。")
            self._update_controls()

    def skip_failed(self):
        self.voice.skip_failed()
        self.status.set("已跳过失败问题。")
        self._update_controls()

    def _poll(self):
        if self.closed:
            return
        self._poll_devices()
        # 先让 ChatSession 提交成功历史，再自动发送下一题。
        for result in self.session.poll():
            self.voice.accept(result)
            if result.kind == "project_selection":
                self.clear_button.configure(state="disabled")
                self._append("面试准备\n", "label")
                self._append("请先确认项目熟悉度顺序，确认后再开始面试。\n", "hint")
                self.status.set("等待确认项目熟悉度 · 最熟悉的项目排在第一位")
                self._show_project_selection()
                continue
            if result.kind == "initialization":
                self.clear_button.configure(state="normal")
                if result.error is not None:
                    self._append("初始化失败\n", "label")
                    self._append(result.error + "\n", "error")
                    self.status.set("准备失败。网络异常可重试；修改简历或 JD 后，请点击重新加载资料。")
                    if sys.stderr is not None:
                        print(f"初始化失败：{result.error}", file=sys.stderr, flush=True)
                else:
                    self.ready_message = result.reply
                    self.transcript.configure(state="normal")
                    self.transcript.delete("1.0", "end")
                    self.transcript.configure(state="disabled")
                    self._show_ready(result.reply)
                    self.status.set("简历、岗位 JD 和项目偏好已接入 · 选择输出设备后点击开始监听")
                    # 同步到启动终端，便于确认实际接口返回，避免只看到空白等待。
                    if sys.stdout is not None:
                        print(result.reply, flush=True)
                continue
            if result.error is not None:
                self._append("未能获得提问\n" if self.session.role == "interviewer" else "未能获得回答\n", "label")
                self._append(result.error + "\n", "error")
                self.status.set("本轮未记入历史。请重试或跳过失败内容，后续转写会继续排队。")
            else:
                self._append("面试官提问\n" if self.session.role == "interviewer" else "面试回答\n", "label")
                self._append(result.reply + "\n")
                self.last_answer = result.reply
                self.copy_button.configure(state="normal", text=self._copy_label())
                self.turn_count.set(f"{len(self.session.history) // 2} 轮对话")
                self.status.set("提问已完成。" if self.session.role == "interviewer" else "回答已完成。")
        for event in self.speech.poll():
            if event.kind == "started":
                self.speech_state.set("正在监听")
                action = "提问" if self.session.role == "interviewer" else "回答"
                self.status.set(f"正在采集：{event.text}；检测到停顿后自动{action}。")
            elif event.kind == "partial":
                self.voice.partial(event.utterance_id, event.text)
                self._set_preview(self.voice.preview)
                self.speech_state.set("正在转写…")
            elif event.kind == "final":
                self.voice.partial(event.utterance_id, event.text)
                self._set_preview(self.voice.preview)
                self.voice.final(event.utterance_id, event.text, event.endpoint)
                self.speech_state.set("正在监听" if event.endpoint else "继续转写…")
            elif event.kind == "error":
                self.voice.pause()
                self._set_preview("")
                self._append("语音识别已停止\n", "label")
                self._append(event.text + "\n", "error")
                self.status.set(event.text + " 未完成的语音未提交，请重新开始监听。")
            elif event.kind == "stopped":
                self.voice.pause()
                self.speech_state.set("已停止")
        try:
            prompt = self.voice.dispatch()
        except Exception as exc:
            self.speech.stop()
            self.voice.pause()
            self.speech_state.set("已暂停")
            self.status.set(f"提交问题失败：{exc}；问题已保留，请重新开始监听。")
        else:
            if prompt is not None:
                self._append("\n候选人的发言\n" if self.session.role == "interviewer" else "\n对方的问题\n", "label")
                self._append(prompt + "\n", "question")
                self.waiting_ticks = 0
        if self.session.busy:
            self.waiting_ticks += 1
            if self.waiting_ticks % 10 == 0:
                operation = ("正在生成提问" if self.session.role == "interviewer" else "正在生成回答"
                             ) if self.session.ready else "正在确认面试资料"
                self.queue_status.set(f"{operation} {self.waiting_ticks // 10} 秒 · 待处理 {self.voice.pending_count} 条")
        else:
            prefix = "等待确认项目顺序" if self.session.awaiting_project_order else (
                "失败内容待处理" if self.voice.failed_prompt else (
                    "停顿后自动提问" if self.session.role == "interviewer" else "停顿后自动回答"))
            self.queue_status.set(f"{prefix} · 待处理 {self.voice.pending_count} 条")
        self._update_controls()
        self.poll_timer = self.root.after(100, self._poll)

    def clear(self):
        if not self.session.ready:
            if self.session.busy:
                return
            if self.project_dialog is not None:
                self.project_dialog.close()
            self.session.reload_materials()
            self.waiting_ticks = 0
            self.status.set("正在重新加载简历和岗位 JD，并识别项目…")
            self._update_controls()
            return
        self.speech.stop()
        self.voice.pause()
        self.voice.clear()
        self.last_answer = ""
        self._set_preview("")
        self.speech_state.set("未开始监听")
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        if self.ready_message:
            self._show_ready(self.ready_message)
        if self.copy_timer is not None:
            self.root.after_cancel(self.copy_timer)
            self.copy_timer = None
        self.copy_button.configure(state="disabled", text=self._copy_label())
        self.turn_count.set("0 轮对话")
        self.status.set("已开始新对话 · 保留面试资料和项目熟悉度，点击开始监听")
        self._update_controls()

    def copy_answer(self):
        if self.last_answer:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.last_answer)
            self.copy_button.configure(text="已复制")
            if self.copy_timer is not None:
                self.root.after_cancel(self.copy_timer)
            self.copy_timer = self.root.after(1800, self._reset_copy_label)

    def _reset_copy_label(self):
        self.copy_timer = None
        self.copy_button.configure(text=self._copy_label())

    def _copy_label(self):
        return "复制最新提问" if self.session.role == "interviewer" else "复制最新回答"

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.speech.close()
        self.session.close()
        for timer in (self.poll_timer, self.copy_timer):
            if timer is not None:
                self.root.after_cancel(timer)
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    FloatingWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
