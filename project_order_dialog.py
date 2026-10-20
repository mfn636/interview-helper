"""面试准备时确认项目熟悉度，不启动语音或提交面试问题。"""

import tkinter as tk
from tkinter import ttk


class ProjectOrderDialog:
    def __init__(self, parent, projects, on_confirm, on_redetect, on_close):
        self.order = list(projects)
        self.on_confirm = on_confirm
        self.on_redetect = on_redetect
        self.on_close = on_close
        self.window = tk.Toplevel(parent, bg="#141414")
        self.window.title("面试准备 · 项目熟悉度")
        self.window.transient(parent)
        self.window.geometry("540x440")
        self.window.minsize(460, 400)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.error = tk.StringVar(master=self.window)
        tk.Label(self.window, text="你最熟悉、最想讲哪个项目？", bg="#141414", fg="#eeeeee",
                 font=("Microsoft YaHei UI", 13, "bold"), anchor="w").pack(fill="x", padx=20, pady=(20, 8))
        explanation = (
            "请按熟悉程度从上到下排序，最熟悉的放第一位。\n当前只是识别顺序，请核对后确认。"
            if self.order else
            "没有识别到明确项目。请检查简历是否完整；可以修改简历后重新识别，或明确确认本次没有项目。"
        )
        description = tk.Label(self.window, text=explanation, bg="#141414", fg="#aaaaaa",
                               font=("Microsoft YaHei UI", 10), anchor="w", justify="left", wraplength=490)
        description.pack(fill="x", padx=20, pady=(0, 12))
        self.window.bind("<Configure>", lambda event: description.configure(
            wraplength=max(350, event.width - 40)) if event.widget is self.window else None)
        container = tk.Frame(self.window, bg="#141414")
        container.pack(fill="both", expand=True, padx=20)
        self.listbox = tk.Listbox(container, bg="#080808", fg="#eeeeee", selectbackground="#484848",
                                 selectforeground="#ffffff", font=("Microsoft YaHei UI", 10),
                                 selectmode="browse", exportselection=False, highlightthickness=0,
                                 borderwidth=0, activestyle="none", height=5)
        vertical = ttk.Scrollbar(container, orient="vertical", command=self.listbox.yview)
        vertical.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=self.listbox.xview)
        horizontal.pack(side="bottom", fill="x")
        self.listbox.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.listbox.pack(fill="both", expand=True)
        controls = tk.Frame(self.window, bg="#141414")
        controls.pack(fill="x", padx=20, pady=10)
        self.up_button = self._button(controls, "上移", lambda: self.move(-1))
        self.up_button.pack(side="left")
        self.down_button = self._button(controls, "下移", lambda: self.move(1))
        self.down_button.pack(side="left", padx=8)
        self._button(controls, "修改简历后重新识别", self.redetect).pack(side="right")
        self.listbox.bind("<<ListboxSelect>>", lambda event: self._update_moves())
        self.listbox.bind("<Alt-Up>", lambda event: self.move(-1))
        self.listbox.bind("<Alt-Down>", lambda event: self.move(1))
        tk.Label(self.window, textvariable=self.error, bg="#141414", fg="#eeeeee",
                 font=("Microsoft YaHei UI", 9), wraplength=480, anchor="w").pack(fill="x", padx=20)
        footer = tk.Frame(self.window, bg="#141414")
        footer.pack(fill="x", padx=20, pady=(8, 18))
        self._button(footer, "稍后确认", self.close).pack(side="left")
        self._button(footer, "确认顺序并准备" if self.order else "确认没有项目并准备",
                     self.confirm, primary=True).pack(side="right")
        self._render(0)
        self.window.grab_set()

    @staticmethod
    def _button(parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0,
                         font=("Microsoft YaHei UI", 10), padx=10, pady=7, cursor="hand2",
                         bg="#dedede" if primary else "#292929", fg="#141414" if primary else "#eeeeee",
                         activebackground="#aaaaaa", disabledforeground="#777777")

    def _render(self, selection):
        self.listbox.delete(0, "end")
        for index, name in enumerate(self.order, 1):
            self.listbox.insert("end", f"{index}. {name}")
        if self.order:
            self.listbox.selection_set(selection)
            self.listbox.activate(selection)
            self.listbox.see(selection)
        self._update_moves()

    def _update_moves(self):
        selected = self.listbox.curselection()
        index = selected[0] if selected else -1
        self.up_button.configure(state="normal" if index > 0 else "disabled")
        self.down_button.configure(state="normal" if 0 <= index < len(self.order) - 1 else "disabled")

    def move(self, delta):
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        target = index + delta
        if 0 <= target < len(self.order):
            self.order[index], self.order[target] = self.order[target], self.order[index]
            self._render(target)

    def confirm(self):
        try:
            self.on_confirm(tuple(self.order))
        except Exception as exc:
            self.error.set(str(exc))
            return
        self.close()

    def redetect(self):
        try:
            self.on_redetect()
        except Exception as exc:
            self.error.set(str(exc))
            return
        self.close()

    def close(self):
        self.window.grab_release()
        self.window.destroy()
        self.on_close()

    def show(self):
        self.window.lift()
        self.window.grab_set()
        self.window.focus_set()
