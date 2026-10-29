"""桌面常驻卡片：状态 / 开关机 / 模式 / 风速。

- 纯标准库（tkinter + urllib），不新增依赖
- 无边框、置顶、不占任务栏，可拖动，位置与置顶状态记在 widget_state.json
- 数据全部走本地 Web 服务的 HTTP 接口，不直接碰 6444 协议
- 请求在子线程执行，UI 不阻塞；写操作以设备回读值（applied）为准

用法：pythonw midea_widget.py     （pythonw 可避免黑色控制台窗口）
"""
import ctypes
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk

BASE = Path(__file__).parent
URL = "http://127.0.0.1:8765"
STATE = BASE / "widget_state.json"
POLL_MS = 5000

ACCENT = "#2f7fe0"
BG = "#ffffff"
FG = "#1f2430"
SUB = "#8b93a1"

MODES = {"cool": "制冷", "heat": "制热", "dry": "除湿", "fan": "送风", "auto": "自动"}
FANS = {"auto": "自动", "silent": "静音", "low": "低", "medium": "中", "high": "高", "max": "强劲"}


def http(path: str, payload: dict | None = None):
    """返回 (状态码, JSON)；HTTP 错误也解析 body，方便识别 428。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body or "{}")
        except json.JSONDecodeError:
            return e.code, {}


class Card:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = None
        self.busy = False
        self.setup_mode = False
        self._dx = self._dy = 0

        pos = self._load_pos()
        root.geometry(f"320x152+{pos.get('x', 60)}+{pos.get('y', 60)}")
        root.overrideredirect(True)
        root.attributes("-topmost", bool(pos.get("topmost", True)))
        root.attributes("-alpha", 0.97)
        root.configure(bg=BG)

        self.top_var = tk.BooleanVar(value=bool(pos.get("topmost", True)))
        self._build()
        self._bind_drag()
        root.after(120, self._tool_window)
        root.after(300, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------- 外观 ----------
    def _build(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=12, pady=(9, 0))
        self.dot = tk.Canvas(top, width=8, height=8, bg=BG, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(1, 1, 7, 7, fill="#c7ccd4", outline="")

        self.name = tk.Label(top, text="美的空调", bg=BG, fg=SUB, font=("Microsoft YaHei UI", 9))
        self.name.pack(side="left", padx=6)

        close = tk.Label(top, text="✕", bg=BG, fg="#a8aeb8", font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.quit())
        more = tk.Label(top, text="⋯", bg=BG, fg="#a8aeb8", font=("Segoe UI", 11), cursor="hand2")
        more.pack(side="right", padx=8)
        more.bind("<Button-1>", self.menu)

        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill="x", padx=12, pady=(2, 0))
        self.temp = tk.Label(mid, text="--℃", bg=BG, fg=FG,
                             font=("Microsoft YaHei UI", 25, "bold"))
        self.temp.pack(side="left")

        self.pw = tk.Canvas(mid, width=50, height=50, bg=BG, highlightthickness=0, cursor="hand2")
        self.pw.pack(side="right", pady=(2, 0))
        self._paint_power(False)
        self.pw.bind("<Button-1>", lambda e: self.toggle_power())

        self.sub = tk.Label(self.root, text="正在连接…", bg=BG, fg=SUB,
                            font=("Microsoft YaHei UI", 9))
        self.sub.pack(fill="x", padx=12)
        self.sub.bind("<Button-1>", lambda e: self._open_console())

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=12, pady=(5, 9))
        self.mode_btn = tk.Label(row, text="模式", bg="#f3f5f9", fg=ACCENT, padx=10, pady=3,
                                 font=("Microsoft YaHei UI", 9), cursor="hand2")
        self.mode_btn.pack(side="left")
        self.fan_btn = tk.Label(row, text="风速", bg="#f3f5f9", fg=ACCENT, padx=10, pady=3,
                                font=("Microsoft YaHei UI", 9), cursor="hand2")
        self.fan_btn.pack(side="left", padx=6)
        self.mode_btn.bind("<Button-1>", lambda e: self.pick_mode())
        self.fan_btn.bind("<Button-1>", lambda e: self.pick_fan())

    def _paint_power(self, on: bool):
        self.pw.delete("all")
        fill = ACCENT if on else "#ffffff"
        outline = ACCENT if on else "#dfe3ea"
        glyph = "#ffffff" if on else "#9aa3b2"
        self.pw.create_oval(3, 3, 47, 47, fill=fill, outline=outline, width=1.5)
        self.pw.create_arc(15, 14, 35, 34, start=120, extent=300, style="arc",
                           outline=glyph, width=2.2)
        self.pw.create_line(25, 14, 25, 26, fill=glyph, width=2.2)

    def _tool_window(self):
        """不占任务栏、不出现在 Alt+Tab。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80)
        except Exception:
            pass

    def _bind_drag(self):
        for w in (self.root, self.name, self.temp, self.sub):
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._do_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())

    def _start_move(self, e):
        self._dx, self._dy = e.x, e.y

    def _do_move(self, e):
        x = self.root.winfo_pointerx() - self._dx
        y = self.root.winfo_pointery() - self._dy
        self.root.geometry(f"+{x}+{y}")

    # ---------- 数据 ----------
    def poll(self):
        def work():
            try:
                code, s = http("/api/status")
            except Exception:
                self.root.after(0, self.offline, "离线 · 点击重试", False)
                return
            if code == 200:
                self.root.after(0, self.apply, s)
            elif code == 428:
                self.root.after(0, self.offline, "未配置 · 点击设置", True)
            else:
                self.root.after(0, self.offline, f"服务错误 {code}", False)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(POLL_MS, self.poll)

    def apply(self, s: dict):
        self.state = s
        self.setup_mode = False
        self._set_dot("#3bb273")
        self.name.config(text=s.get("model") or "美的空调")
        self.temp.config(text="--℃" if s.get("target") is None
                         else f"{round(float(s['target']))}℃")
        indoor = s.get("indoor")
        mode = MODES.get(s.get("mode"), s.get("mode", ""))
        fan = s.get("fan_pct")
        fan_txt = f"{fan}%" if fan is not None else FANS.get(s.get("fan"), s.get("fan", ""))
        self.sub.config(text=f"室内 {indoor:.1f}℃　{mode} · {fan_txt}" if indoor is not None
                        else f"{mode} · {fan_txt}")
        self._paint_power(bool(s.get("power")))
        self.mode_btn.config(text=mode or "模式")
        self.fan_btn.config(text=f"风速 {fan_txt}" if fan_txt else "风速")

    def offline(self, msg: str, setup: bool):
        self.setup_mode = setup
        self._set_dot("#c7ccd4" if not setup else "#e0a63c")
        self.sub.config(text=msg)
        self.mode_btn.config(text="模式")
        self.fan_btn.config(text="风速")

    def _set_dot(self, color: str):
        self.dot.itemconfig(self.dot_id, fill=color)

    # ---------- 操作 ----------
    def toggle_power(self):
        if not self.state:
            return self._open_console()
        self.control({"power": not self.state.get("power")})

    def pick_mode(self):
        if not self.state:
            return self._open_console()
        m = tk.Menu(self.root, tearoff=0)
        for k, label in MODES.items():
            m.add_command(label=label, command=lambda k=k: self.control({"mode": k}))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def pick_fan(self):
        if not self.state:
            return self._open_console()
        m = tk.Menu(self.root, tearoff=0)
        for k, label in FANS.items():
            m.add_command(label=label, command=lambda k=k: self.control({"fan": k}))
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def control(self, payload: dict):
        if self.busy:
            return
        self.busy = True
        self._set_dot("#e0a63c")

        def work():
            try:
                code, s = http("/api/control", payload)
                if code == 200:
                    self.root.after(0, self.apply, s)
                    if s.get("applied") is False:      # 设备没接受：以回读值为准并提示
                        self.root.after(0, self.flash, "指令未生效")
                elif code == 428:
                    self.root.after(0, self.offline, "未配置 · 点击设置", True)
                else:
                    self.root.after(0, self.flash, s.get("error") or f"失败 {code}")
            except Exception:
                self.root.after(0, self.offline, "离线 · 点击重试", False)
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()

    def flash(self, msg: str):
        """短暂提示，1.5 秒后恢复原状态文字。"""
        last = self.sub.cget("text")
        self.sub.config(text=msg)
        self.root.after(1500, lambda: self.sub.cget("text") == msg
                        and self.sub.config(text=last))

    def _open_console(self):
        webbrowser.open(URL)

    # ---------- 菜单 / 生命周期 ----------
    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_checkbutton(label="窗口置顶", variable=self.top_var, command=self.toggle_top)
        m.add_command(label="打开完整控制台", command=self._open_console)
        m.add_command(label="创建开机自启", command=self.install_startup)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def toggle_top(self):
        self.root.attributes("-topmost", self.top_var.get())
        self._save_pos()

    def install_startup(self):
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        bat = startup / "midea_widget.bat"
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        bat.write_text(f'@echo off\r\nstart "" "{pyw}" "{BASE / "midea_widget.py"}"\r\n',
                       encoding="utf-8")
        self.flash(f"已创建开机自启：{bat.name}")

    def _load_pos(self) -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_pos(self):
        try:
            STATE.write_text(json.dumps({
                "x": self.root.winfo_x(), "y": self.root.winfo_y(),
                "topmost": self.top_var.get(),
            }), encoding="utf-8")
        except Exception:
            pass

    def quit(self):
        self._save_pos()
        self.root.destroy()


def main():
    try:                          # 高分屏下字体不发虚
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("美的空调")
    Card(root)
    root.mainloop()


if __name__ == "__main__":
    main()
