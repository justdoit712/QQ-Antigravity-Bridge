"""Small desktop controller for the official QQ Bot / Antigravity bridge."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from config_store import ROOT, Config, load_config, save_config


class BridgeWindow(tk.Tk):
    """Keep all Tk operations on the main thread and backend output in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.title("doro QQ / Antigravity 桥接")
        self.geometry("820x760")
        self.minsize(730, 700)
        self.configure(bg="#f7f5fa")

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=500)
        self._proc: subprocess.Popen[str] | None = None
        self._mode: str | None = None
        self._closing = False
        self._disposed = False
        self._stop_requested = False
        self._force_stop_id: str | None = None
        self._kill_id: str | None = None
        self._active_secret = ""
        self._owner_id = ""
        self._group_count = 0
        self._private_user_count = 0
        self._checked = False
        self._process_error = False
        self._unrecognized_output = False
        self._editable: list[Any] = []

        config_error = False
        try:
            config = load_config()
        except Exception:
            config = Config()
            config_error = True

        self.app_id = tk.StringVar(value=config.app_id)
        self.app_secret = tk.StringVar(value=config.app_secret)
        self.model = tk.StringVar(value=config.model)
        self.show_secret = tk.BooleanVar(value=False)
        self.owner_status = tk.StringVar()
        self.status = tk.StringVar(value="尚未启动")
        self.pair_code = tk.StringVar()
        self.pair_hint = tk.StringVar(value="尚未配对时，启动桥接后会在这里显示配对码。")
        self._owner_id = config.owner_id
        self._group_count = len(config.group_ids)
        self._private_user_count = len(config.private_user_ids)

        self._configure_style()
        self._build(config.persona)
        self._show_owner()
        self._set_controls()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_events)
        if config_error:
            self.after(
                100,
                lambda: messagebox.showerror(
                    "无法读取设置",
                    "配置读取失败，请检查配置文件或重新填写设置。现有配置尚未被修改。",
                    parent=self,
                ),
            )

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#f7f5fa")
        style.configure("TLabel", background="#f7f5fa", foreground="#2d2737", font=("Microsoft YaHei UI", 9))
        style.configure("Muted.TLabel", foreground="#756d80", font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", foreground="#4e355f", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 6))
        style.configure("Accent.TButton", background="#e5c9df", foreground="#3b2544")
        style.map("Accent.TButton", background=[("active", "#dcb6d5"), ("disabled", "#e7e1e9")])
        style.configure("TEntry", padding=5, font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background="#f7f5fa", font=("Microsoft YaHei UI", 9))

    def _build(self, persona: str) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body = ttk.Frame(self, padding=(22, 12))
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(8, weight=1)

        ttk.Label(body, text="doro", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="QQ / Antigravity 桥接", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(1, 6)
        )
        instructions = ttk.Label(
            body,
            text=(
                "首次启动后，私聊发送配对码成为管理员；私聊 /invite 可邀请其他 QQ 使用。保持本窗口运行。\n"
                "群聊：私聊发送 /group 获取绑定码，再在目标群 @doro /bind 绑定码；绑定后 @doro 提问。"
            ),
            wraplength=760,
            justify="left",
        )
        instructions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        instructions.bind("<Configure>", lambda event: instructions.configure(wraplength=max(320, event.width)))

        form = ttk.Frame(body)
        form.grid(row=3, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="AppID").grid(row=0, column=0, sticky="w", padx=(0, 16), pady=(0, 7))
        id_entry = ttk.Entry(form, textvariable=self.app_id)
        id_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 7))
        self._editable.append(id_entry)
        ttk.Label(form, text="AppSecret").grid(row=1, column=0, sticky="w", padx=(0, 16), pady=(0, 7))
        self.secret_entry = ttk.Entry(form, textvariable=self.app_secret, show="●")
        self.secret_entry.grid(row=1, column=1, sticky="ew", pady=(0, 7))
        show = ttk.Checkbutton(form, text="显示", variable=self.show_secret, command=self._toggle_secret)
        show.grid(row=1, column=2, padx=(10, 0), pady=(0, 7))
        self._editable.extend([self.secret_entry, show])
        ttk.Label(form, text="模型").grid(row=2, column=0, sticky="w", padx=(0, 16))
        model_entry = ttk.Entry(form, textvariable=self.model)
        model_entry.grid(row=2, column=1, columnspan=2, sticky="ew")
        self._editable.append(model_entry)
        ttk.Label(form, text="可选；留空使用 Antigravity 默认模型。", style="Muted.TLabel").grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(3, 7)
        )

        persona_frame = ttk.Frame(body)
        persona_frame.grid(row=4, column=0, sticky="ew")
        persona_frame.columnconfigure(0, weight=1)
        ttk.Label(persona_frame, text="doro 的说话风格", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.persona = tk.Text(
            persona_frame,
            height=3,
            wrap="word",
            font=("Microsoft YaHei UI", 9),
            relief="solid",
            bd=1,
            padx=10,
            pady=5,
            undo=True,
        )
        self.persona.grid(row=1, column=0, sticky="ew")
        persona_scroll = ttk.Scrollbar(persona_frame, orient="vertical", command=self.persona.yview)
        persona_scroll.grid(row=1, column=1, sticky="ns")
        self.persona.configure(yscrollcommand=persona_scroll.set)
        self.persona.insert("1.0", persona)
        self._editable.append(self.persona)

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="ew", pady=(8, 8))
        self.start_button = ttk.Button(actions, text="启动桥接", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self._request_stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.check_button = ttk.Button(actions, text="测试 Antigravity", command=self._check_agy)
        self.check_button.pack(side="left", padx=(8, 0))
        self.save_button = ttk.Button(actions, text="保存设置", command=self._save_clicked)
        self.save_button.pack(side="right")

        pair_frame = tk.Frame(body, bg="#eee4f1", padx=12, pady=8)
        pair_frame.grid(row=6, column=0, sticky="ew", pady=(0, 6))
        pair_frame.columnconfigure(0, weight=1)
        tk.Label(
            pair_frame,
            text="QQ 配对码",
            bg="#eee4f1",
            fg="#4e355f",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        code_entry = ttk.Entry(pair_frame, textvariable=self.pair_code, state="readonly", font=("Consolas", 17, "bold"))
        code_entry.grid(row=1, column=0, sticky="ew", pady=(4, 3))
        self.copy_button = ttk.Button(pair_frame, text="复制配对码", command=self._copy_pair_code)
        self.copy_button.grid(row=1, column=1, padx=(12, 0))
        tk.Label(
            pair_frame,
            textvariable=self.pair_hint,
            bg="#eee4f1",
            fg="#756080",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            wraplength=680,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="ew")

        owner_row = ttk.Frame(body)
        owner_row.grid(row=7, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(owner_row, textvariable=self.owner_status, style="Muted.TLabel").pack(side="left")
        self.unpair_button = ttk.Button(owner_row, text="解除配对", command=self._unpair)
        self.unpair_button.pack(side="right")

        log_frame = ttk.Frame(body)
        log_frame.grid(row=8, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="运行消息", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log = tk.Text(
            log_frame,
            height=4,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 9),
            relief="flat",
            padx=10,
            pady=5,
            bg="#ffffff",
            fg="#4e4659",
        )
        self.log.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        ttk.Label(body, textvariable=self.status, style="Section.TLabel").grid(
            row=9, column=0, sticky="w", pady=(8, 0)
        )

    def _toggle_secret(self) -> None:
        self.secret_entry.configure(show="" if self.show_secret.get() else "●")

    def _safe_message(self, value: Any) -> str:
        text = str(value)
        for secret in (self.app_secret.get().strip(), self._active_secret):
            if secret:
                text = text.replace(secret, "[密钥已隐藏]")
        return text[:4000]

    def _append_log(self, message: str) -> None:
        message = self._safe_message(message)
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n")
        # The window retains only a small in-memory tail; no log file is created.
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 200:
            self.log.delete("1.0", f"{line_count - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_owner(self) -> None:
        owner = "管理员已配对" if self._owner_id else "尚未配对 QQ 账号"
        private_count = self._private_user_count + bool(self._owner_id)
        self.owner_status.set(f"{owner} · 私聊账号 {private_count} 个 · 已绑定 {self._group_count} 个群")
        if self._owner_id:
            self.pair_code.set("")
            self.pair_hint.set("管理员已配对，私聊 /invite 可邀请其他 QQ。更换管理员前请停止桥接并解除配对。")

    def _refresh_owner(self) -> None:
        try:
            config = load_config()
            self._owner_id = config.owner_id
            self._group_count = len(config.group_ids)
            self._private_user_count = len(config.private_user_ids)
        except Exception:
            return
        self._show_owner()

    def _save(self) -> bool:
        if self._proc is not None or self._closing:
            return False
        try:
            # The backend can update owner/group bindings. Preserve its newest
            # values unless the AppID changes, since OpenIDs are app-specific.
            latest = load_config()
            app_id = self.app_id.get().strip()
            owner_id = latest.owner_id if app_id == latest.app_id else ""
            config = replace(
                latest,
                app_id=app_id,
                app_secret=self.app_secret.get().strip(),
                model=self.model.get().strip(),
                persona=self.persona.get("1.0", "end-1c").strip(),
                owner_id=owner_id,
                group_ids=list(latest.group_ids) if app_id == latest.app_id else [],
                private_user_ids=list(latest.private_user_ids) if app_id == latest.app_id else [],
            )
            save_config(config)
            self._owner_id = owner_id
            self._group_count = len(config.group_ids)
            self._private_user_count = len(config.private_user_ids)
            if app_id != latest.app_id:
                self.pair_code.set("")
                self.pair_hint.set("机器人已更换，启动后请使用新的配对码重新配对。")
            self._show_owner()
            return True
        except Exception:
            messagebox.showerror("无法保存", "设置保存失败，请检查配置文件和目录权限。", parent=self)
            return False

    def _save_clicked(self) -> None:
        if self._save():
            self.status.set("设置已保存")
            self._append_log("设置已保存，AppSecret 由本机加密保护。")
            self._set_controls()

    def _unpair(self) -> None:
        if self._proc is not None or self._closing:
            return
        try:
            config = load_config()
            save_config(replace(config, owner_id="", group_ids=[], private_user_ids=[]))
        except Exception:
            messagebox.showerror("无法解除配对", "配置保存失败，配对状态未更改。", parent=self)
            return
        self._owner_id = ""
        self._group_count = 0
        self._private_user_count = 0
        self.pair_code.set("")
        self.pair_hint.set("配对已解除；下次启动后，请在 QQ 私聊 doro 发送新的配对码。")
        self._show_owner()
        self.status.set("已解除配对")
        self._append_log("配对、私聊授权及群绑定已解除。下次启动时将生成新的配对码。")
        self._set_controls()

    def _copy_pair_code(self) -> None:
        code = self.pair_code.get()
        if not code:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(code)
            self.status.set("配对码已复制，请在 QQ 私聊 doro 发送")
        except tk.TclError:
            self.status.set("复制失败，请手动选择配对码复制")

    def _start(self) -> None:
        if not self.app_id.get().strip() or not self.app_secret.get().strip():
            messagebox.showinfo("请填写机器人信息", "请先填写 QQ 官方机器人的 AppID 和 AppSecret。", parent=self)
            return
        self._launch("run")

    def _check_agy(self) -> None:
        self._launch("check-agy")

    def _launch(self, mode: str) -> None:
        if self._proc is not None or self._closing or not self._save():
            return
        bridge = ROOT / "bridge.py"
        if not bridge.is_file():
            messagebox.showerror("无法启动", "安装目录中未找到 bridge.py，请检查程序文件是否完整。", parent=self)
            return
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        self._active_secret = self.app_secret.get().strip()
        self._mode = mode
        self._checked = False
        self._process_error = False
        self._stop_requested = False
        self._unrecognized_output = False
        self.show_secret.set(False)
        self._toggle_secret()
        self.pair_code.set("")
        if not self._owner_id:
            self.pair_hint.set("正在启动，请稍候…" if mode == "run" else "测试通过后，再启动桥接获取配对码。")
        try:
            interpreter = Path(sys.executable)
            if interpreter.name.lower() == "pythonw.exe":
                interpreter = interpreter.with_name("python.exe")
                if not interpreter.is_file():
                    raise FileNotFoundError("Python console interpreter is unavailable")
            proc = subprocess.Popen(
                [str(interpreter), "-u", str(bridge), mode],
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self._active_secret = ""
            self._mode = None
            self.status.set("启动失败")
            messagebox.showerror("启动失败", "无法启动后台程序，请检查 Python 和程序文件是否可用。", parent=self)
            return
        self._proc = proc
        self.status.set("正在启动桥接…" if mode == "run" else "正在测试 Antigravity…")
        self._append_log("启动 QQ 桥接。" if mode == "run" else "测试 Antigravity 连接。")
        self._set_controls()
        threading.Thread(target=self._read_backend, args=(proc,), daemon=True, name="bridge-output").start()

    def _read_backend(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("Expected an event object")
                    except (ValueError, TypeError):
                        # Never echo raw stderr: it might contain credentials.
                        self._events.put(("unknown", proc))
                        continue
                    self._events.put(("event", (proc, event)))
        except Exception:
            self._events.put(("reader_error", proc))
        finally:
            returncode = proc.wait()
            self._events.put(("exit", (proc, returncode)))

    def _drain_events(self) -> None:
        for _ in range(100):
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                proc, event = payload
                if proc is self._proc:
                    self._handle_event(event)
            elif kind == "unknown" and payload is self._proc:
                if not self._unrecognized_output:
                    self._unrecognized_output = True
                    self._append_log("后台返回了非标准运行消息；已隐藏原始输出。")
            elif kind == "reader_error" and payload is self._proc:
                self._append_log("后台消息读取中断，正在等待程序退出。")
            elif kind == "exit":
                self._handle_exit(*payload)
            if self._disposed:
                return
        self.after(80, self._drain_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind not in {"status", "error", "pair", "ready", "stopped", "checked"}:
            return
        message = self._safe_message(event.get("message") or "")
        if message:
            self._append_log(message)
            if not self._stop_requested:
                self.status.set(message.replace("\n", " ")[:180])
        if kind == "error":
            self._process_error = True
        elif kind == "checked":
            self._checked = True
        elif kind == "pair":
            code = str(event.get("code") or "").strip()
            if code:
                self.pair_code.set(code[:128])
                self.pair_hint.set("用你自己的 QQ 私聊 doro，发送上面的完整配对码。")
        self._refresh_owner()
        self._set_controls()

    def _set_controls(self) -> None:
        busy = self._proc is not None or self._closing
        for widget in self._editable:
            widget.configure(state="disabled" if busy else "normal")
        for button in (self.start_button, self.check_button, self.save_button):
            button.configure(state="disabled" if busy else "normal")
        self.unpair_button.configure(state="normal" if self._owner_id and not busy else "disabled")
        self.stop_button.configure(
            state="normal" if self._proc is not None and not self._stop_requested and not self._closing else "disabled",
            text="正在停止…" if self._stop_requested else "停止",
        )
        self.copy_button.configure(state="normal" if self.pair_code.get() and not self._closing else "disabled")

    def _request_stop(self) -> None:
        self._refresh_owner()
        proc = self._proc
        if proc is None:
            if self._closing:
                self._finish_close()
            return
        if self._stop_requested:
            return
        self._stop_requested = True
        self.status.set("正在停止后台，请稍候…")
        self._set_controls()
        self._append_log("正在停止后台。")

        def send_stop() -> None:
            try:
                if proc.stdin is not None and proc.poll() is None:
                    proc.stdin.write("stop\n")
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass

        threading.Thread(target=send_stop, daemon=True, name="bridge-stop").start()
        self._force_stop_id = self.after(15_000, lambda: self._force_stop(proc))

    def _force_stop(self, proc: subprocess.Popen[str]) -> None:
        self._force_stop_id = None
        if proc is not self._proc or proc.poll() is not None:
            return
        self._append_log("后台未及时停止，正在结束进程。")
        threading.Thread(target=self._terminate_tree, args=(proc, False), daemon=True, name="bridge-terminate").start()
        self._kill_id = self.after(10_000, lambda: self._kill_backend(proc))

    @staticmethod
    def _terminate_tree(proc: subprocess.Popen[str], force_kill: bool) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=7,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            try:
                proc.kill() if force_kill else proc.terminate()
            except OSError:
                pass

    def _kill_backend(self, proc: subprocess.Popen[str]) -> None:
        self._kill_id = None
        if proc is self._proc and proc.poll() is None:
            threading.Thread(target=self._terminate_tree, args=(proc, True), daemon=True, name="bridge-kill").start()

    def _handle_exit(self, proc: subprocess.Popen[str], returncode: int) -> None:
        if proc is not self._proc:
            return
        for timer in (self._force_stop_id, self._kill_id):
            if timer is not None:
                self.after_cancel(timer)
        self._force_stop_id = None
        self._kill_id = None
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        stopped_by_user = self._stop_requested
        mode = self._mode
        self._proc = None
        self._mode = None
        self._stop_requested = False
        self._refresh_owner()
        self.pair_code.set("")
        if not self._owner_id:
            self.pair_hint.set("桥接已停止。下次启动后，请使用新显示的配对码。")
        if stopped_by_user:
            self.status.set("已停止")
        elif returncode != 0 or self._process_error:
            self.status.set("后台已退出，请查看上方运行消息")
        elif mode == "check-agy" and self._checked:
            self.status.set("Antigravity 测试通过，可以启动桥接")
        elif mode == "check-agy":
            self.status.set("测试已结束，未收到测试通过确认")
        else:
            self.status.set("桥接已停止")
        self._append_log("后台已停止。" if stopped_by_user or returncode == 0 else f"后台已退出（代码 {returncode}）。")
        self._active_secret = ""
        if self._closing:
            self._finish_close()
            return
        self._set_controls()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._proc is not None:
            self._request_stop()
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        self._disposed = True
        self.destroy()


def main() -> None:
    BridgeWindow().mainloop()


if __name__ == "__main__":
    main()
