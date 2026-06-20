"""
라씨 매매비서 — 바탕화면 실행기
더블클릭으로 실행하면 Flask 서버가 뜨고 브라우저가 자동으로 열립니다.
"""
import sys
import os
import threading
import subprocess
import webbrowser
import tkinter as tk
from tkinter import scrolledtext, ttk
import queue
import time

# 프로젝트 루트를 sys.path에 추가
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

PORT = 5000
URL  = f"http://127.0.0.1:{PORT}"

class LassiApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("라씨 매매비서")
        self.geometry("700x500")
        self.resizable(True, True)
        self.configure(bg="#0d1117")

        self._server_thread = None
        self._log_queue = queue.Queue()
        self._running = False

        self._build_ui()
        self._redirect_stdout()
        self._start_server()

        # 큐에서 로그 폴링
        self.after(200, self._poll_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 상단 타이틀 + 상태
        top = tk.Frame(self, bg="#0d1117")
        top.pack(fill="x", padx=16, pady=(14, 6))

        tk.Label(top, text="라씨 매매비서", font=("맑은 고딕", 16, "bold"),
                 fg="#58a6ff", bg="#0d1117").pack(side="left")

        self._status_var = tk.StringVar(value="● 시작 중...")
        self._status_label = tk.Label(top, textvariable=self._status_var,
                                       font=("맑은 고딕", 10), fg="#f59e0b", bg="#0d1117")
        self._status_label.pack(side="left", padx=14)

        # 버튼
        btn_frame = tk.Frame(top, bg="#0d1117")
        btn_frame.pack(side="right")

        self._open_btn = tk.Button(
            btn_frame, text="🌐 브라우저 열기",
            command=self._open_browser,
            font=("맑은 고딕", 9), bg="#1d4ed8", fg="white",
            relief="flat", padx=12, pady=4, cursor="hand2"
        )
        self._open_btn.pack(side="left", padx=4)

        self._quit_btn = tk.Button(
            btn_frame, text="✕ 종료",
            command=self._on_close,
            font=("맑은 고딕", 9), bg="#7f1d1d", fg="white",
            relief="flat", padx=12, pady=4, cursor="hand2"
        )
        self._quit_btn.pack(side="left", padx=4)

        # 구분선
        tk.Frame(self, bg="#21262d", height=1).pack(fill="x", padx=0)

        # 로그 창
        log_frame = tk.Frame(self, bg="#0d1117")
        log_frame.pack(fill="both", expand=True, padx=12, pady=10)

        tk.Label(log_frame, text="서버 로그", font=("맑은 고딕", 9),
                 fg="#4b5563", bg="#0d1117").pack(anchor="w")

        self._log_box = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", 9),
            bg="#010409", fg="#c9d1d9",
            insertbackground="#c9d1d9",
            relief="flat", bd=0,
            state="disabled",
            wrap="word",
        )
        self._log_box.pack(fill="both", expand=True, pady=(4, 0))

        # 태그 색상
        self._log_box.tag_config("err",  foreground="#f85149")
        self._log_box.tag_config("warn", foreground="#f59e0b")
        self._log_box.tag_config("info", foreground="#58a6ff")
        self._log_box.tag_config("ok",   foreground="#3fb950")

        # 하단 URL 표시
        bottom = tk.Frame(self, bg="#161b22")
        bottom.pack(fill="x", padx=0, pady=0)
        tk.Label(bottom, text=f"접속 주소: {URL}",
                 font=("Consolas", 9), fg="#4b5563", bg="#161b22",
                 anchor="w").pack(side="left", padx=12, pady=6)

    def _redirect_stdout(self):
        """stdout/stderr를 큐로 리디렉트."""
        import io
        class _Writer(io.TextIOBase):
            def __init__(self, q): self._q = q
            def write(self, s):
                if s.strip(): self._q.put(s)
                return len(s)
            def flush(self): pass

        sys.stdout = _Writer(self._log_queue)
        sys.stderr = _Writer(self._log_queue)

    def _start_server(self):
        def _run():
            try:
                # logging 출력도 큐로
                import logging
                class _QHandler(logging.Handler):
                    def __init__(self, q): super().__init__(); self._q = q
                    def emit(self, record):
                        self._q.put(self.format(record) + "\n")
                _h = _QHandler(self._log_queue)
                _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                                   datefmt="%H:%M:%S"))
                logging.getLogger().addHandler(_h)
                logging.getLogger().setLevel(logging.INFO)

                from base.app import app
                self._running = True
                self.after(0, self._on_server_ready)
                app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
            except Exception as e:
                self._log_queue.put(f"[오류] 서버 시작 실패: {e}\n")
                self.after(0, lambda: self._set_status(f"● 오류: {e}", "#f85149"))

        self._server_thread = threading.Thread(target=_run, daemon=True)
        self._server_thread.start()

    def _on_server_ready(self):
        self._set_status("● 서버 실행 중", "#3fb950")
        # 1초 후 브라우저 자동 오픈
        self.after(1000, self._open_browser)

    def _open_browser(self):
        webbrowser.open(URL)

    def _set_status(self, text, color):
        self._status_var.set(text)
        self._status_label.configure(fg=color)

    def _poll_log(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(200, self._poll_log)

    def _append_log(self, line: str):
        self._log_box.configure(state="normal")
        tag = "info"
        low = line.lower()
        if "error" in low or "오류" in low or "실패" in low:
            tag = "err"
        elif "warning" in low or "warn" in low or "주의" in low:
            tag = "warn"
        elif "완료" in low or "성공" in low or "발급" in low:
            tag = "ok"
        self._log_box.insert("end", line if line.endswith("\n") else line + "\n", tag)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _on_close(self):
        self._set_status("● 종료 중...", "#4b5563")
        self.after(300, self.destroy)


if __name__ == "__main__":
    app = LassiApp()
    app.mainloop()
