"""
C.H.A.O.S. Control Panel - desktop application.

A graphical front end for the CHAOS engine (CHAOS.py). The engine still does all the
work (speech recognition, OS control, the local AI, True Sight, the database); this file
adds the 9 screens designed in Deliverable 4 and runs the engine on background threads:

    S1 Unlock (local PIN)       S4 True Sight          S7 Settings (admin)
    S2 Dashboard                S5 Productivity        S8 Performance (admin)
    S3 Conversation             S6 Memory and History  S9 Error and empty-state banners
    + a System Check screen that finds Ollama and downloads the AI model on first run.

Run:  py -3.11 chaos_app.py      (or the packaged CHAOS.exe)
"""
import collections
import datetime
import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

# ---------------------------------------------------------------- console capture
# The engine prints status lines. A windowed .exe has no console, so keep them in
# memory and show them on the Performance screen's activity log.
LOG_LINES = collections.deque(maxlen=400)


class _UIStream:
    def __init__(self, original):
        self.original = original
        self._buf = ""

    def write(self, text):
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                LOG_LINES.append(f"{time.strftime('%H:%M:%S')}  {line}")
        return len(text)

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass

    def reconfigure(self, **kwargs):  # the engine calls this on Windows
        pass


sys.stdout = _UIStream(sys.stdout)
sys.stderr = _UIStream(sys.stderr)

import tkinter as tk  # noqa: E402
import customtkinter as ctk  # noqa: E402

import CHAOS as E  # noqa: E402  (the engine)

# ---------------------------------------------------------------- look and feel
ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

BG = ("#eef1f6", "#0f131a")
WIN = ("#ffffff", "#161b24")
PANEL = ("#f6f8fb", "#1c2330")
LINE = ("#d5dbe5", "#2c3544")
INK = ("#18202c", "#e6ebf3")
MUTE = ("#5b6677", "#9aa6b8")
ACCENT = ("#2f5bd3", "#7fa2ff")
ACCENT_HOVER = ("#264bb0", "#6b8fef")
ACCENT_SOFT = ("#e3eafc", "#1f2b47")
GOOD = ("#1f7a4d", "#5fd19a")
WARN = ("#9a6400", "#f0b955")
WARN_SOFT = ("#fdf2dc", "#3a2f17")
BAD = ("#b4232c", "#ff7b82")
BAD_SOFT = ("#fbe9ea", "#3a1d22")
INFO_SOFT = ("#e3eafc", "#1f2b47")

FONT = "Segoe UI"


def F(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def col(pair):
    """Pick the light or dark value for plain Tk widgets (canvases)."""
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


def resource(name):
    return os.path.join(E.RES_DIR, name)


# ---------------------------------------------------------------- helpers: Ollama, PIN
OLLAMA = E.ollama_api_url.replace("/api/chat", "")
DEFAULT_MODEL = "llama3.2"


def ollama_models():
    """Installed model names, or None if Ollama is not reachable."""
    try:
        r = E.requests.get(OLLAMA + "/api/tags", timeout=2)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


def find_ollama_exe():
    candidates = [shutil.which("ollama"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                  r"C:\Program Files\Ollama\ollama.exe"]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def start_ollama(wait=20):
    exe = find_ollama_exe()
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[Setup] Couldn't start Ollama: {e}")
        return False
    for _ in range(wait * 2):
        if ollama_models() is not None:
            return True
        time.sleep(0.5)
    return False


def has_model(models, name):
    return any(m == name or m.split(":")[0] == name for m in (models or []))


def pin_record(pin, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()
    return {"salt": salt, "hash": digest}


def pin_matches(pin):
    rec = E.setting_get("pin_hash")
    return bool(rec) and secrets.compare_digest(pin_record(pin, rec["salt"])["hash"], rec["hash"])


def fmt_mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ---------------------------------------------------------------- engine bridge
class EngineBridge:
    """Runs commands and the microphone loop off the UI thread."""

    WAKE_PHRASES = ["chaos initiate", "chaos activate", "chaos wake up", "chaos awaken", "wake up chaos"]

    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.listening = False
        self.locked = False
        self.busy = False
        self.running = True
        self.mic_ok = True
        self.voice_state = "idle"
        E.ui_listeners.append(self._on_reply)
        threading.Thread(target=self._work, daemon=True, name="chaos-commands").start()
        threading.Thread(target=self._voice, daemon=True, name="chaos-voice").start()

    def _on_reply(self, text):
        started = E.command_started_at
        elapsed = time.time() - started if started else None
        self.app.post("reply", (text, elapsed))

    def submit(self, text, lang="en", typed=True):
        text = text.strip()
        if not text:
            return
        self.busy = True
        self.q.put((text, lang, typed))

    def _work(self):
        while self.running:
            text, lang, typed = self.q.get()
            self.busy = True
            self.app.post("busy", True)
            try:
                query = text.lower()
                if typed:
                    E.command_started_at = time.time()
                E.log_conversation("user", text, lang)
                if not E.is_awake:
                    if any(p in query for p in self.WAKE_PHRASES) or "chaos" in query:
                        E.is_awake = True
                        E.speak("Systems online. I am listening.", force_voice=True)
                    else:
                        E.speak("I'm in standby. Say 'CHAOS activate' to wake me.", allow_interrupt=False)
                    continue
                _, should_exit, interrupted, int_text = E.process_command(query, None, lang)
                E.save_preferences()
                if should_exit:
                    self.app.post("exit", None)
                if interrupted and int_text:
                    self.q.put((int_text, "en", False))
            except Exception as e:
                print(f"[Command error: {e}]")
                self.app.post("reply", (f"Something went wrong running that command: {e}", None))
            finally:
                E.command_started_at = None
                self.busy = not self.q.empty()
                self.app.post("busy", False)

    def _voice(self):
        while self.running:
            if not self.listening or self.locked or self.busy or E.is_speaking:
                self._set_state("speaking" if E.is_speaking else ("busy" if self.busy else "idle"))
                time.sleep(0.2)
                continue
            started = time.time()
            self._set_state("listening")
            try:
                heard, lang = E.listen()
            except OSError as e:
                print(f"[Microphone error: {e}]")
                self.mic_ok = False
                self.listening = False
                self.app.post("nomic", str(e))
                continue
            except Exception as e:
                print(f"[Voice loop error: {e}]")
                time.sleep(1)
                continue
            if not heard or not self.listening or self.locked:
                continue
            if E.is_speaking or E.last_speak_time > started:
                continue  # CHAOS heard its own voice
            self.app.post("heard", (heard, lang))
            self.submit(heard, lang, typed=False)

    def _set_state(self, state):
        if state != self.voice_state:
            self.voice_state = state
            self.app.post("voice_state", state)


# ---------------------------------------------------------------- small widgets
class Card(ctk.CTkFrame):
    def __init__(self, master, title=None, **kw):
        super().__init__(master, fg_color=WIN, border_width=1, border_color=LINE, corner_radius=10, **kw)
        if title:
            self.title = ctk.CTkLabel(self, text=title, font=F(14, "bold"), text_color=INK, anchor="w")
            self.title.pack(fill="x", padx=16, pady=(12, 6))


def label(master, text, size=13, weight="normal", color=INK, **kw):
    return ctk.CTkLabel(master, text=text, font=F(size, weight), text_color=color, anchor="w", justify="left", **kw)


def button(master, text, command, primary=False, danger=False, width=None, **kw):
    opts = dict(text=text, command=command, font=F(13, "bold" if primary else "normal"), corner_radius=8, height=32)
    if primary:
        opts.update(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WIN)
    elif danger:
        opts.update(fg_color="transparent", border_width=1, border_color=BAD, text_color=BAD, hover_color=BAD_SOFT)
    else:
        opts.update(fg_color="transparent", border_width=1, border_color=LINE, text_color=INK, hover_color=PANEL)
    if width:
        opts["width"] = width
    opts.update(kw)
    return ctk.CTkButton(master, **opts)


def link(master, text, command):
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", hover_color=PANEL,
                         text_color=ACCENT, font=F(12), height=24, width=10, anchor="w")


def switch(master, text, command, on=False):
    s = ctk.CTkSwitch(master, text=text, command=command, font=F(12), text_color=MUTE,
                      progress_color=ACCENT, onvalue=True, offvalue=False)
    s.select() if on else s.deselect()
    return s


class Screen(ctk.CTkFrame):
    title = ""
    trace = ""

    def __init__(self, app):
        super().__init__(app.content, fg_color="transparent")
        self.app = app
        self.e = app.engine

    def on_show(self):
        pass

    def refresh(self):
        pass


# ---------------------------------------------------------------- S1 Unlock
class UnlockScreen(ctk.CTkFrame):
    def __init__(self, app):
        super().__init__(app.body_host, fg_color="transparent")
        self.app = app
        self.attempts = 5
        self.lock_until = 0
        card = Card(self)
        card.place(relx=0.5, rely=0.45, anchor="center")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(padx=34, pady=28)
        label(inner, "CHAOS is locked", 18, "bold").pack()
        label(inner, "Enter your PIN (4 to 6 digits)", 12, color=MUTE).pack(pady=(2, 12))
        self.entry = ctk.CTkEntry(inner, show="•", width=220, height=40, font=F(20), justify="center",
                                  border_color=LINE)
        self.entry.pack()
        self.entry.bind("<Return>", lambda e: self.submit())
        self.err = label(inner, "", 12, color=BAD)
        self.err.pack(pady=(8, 4))
        button(inner, "Unlock", self.submit, primary=True, width=220).pack()
        label(inner, "Voice input is off while locked", 11, color=MUTE).pack(pady=(12, 0))
        self.relock = label(inner, "", 11, color=MUTE)
        self.relock.pack()

    def on_show(self):
        self.entry.delete(0, "end")
        mins = E.setting_get("auto_lock_minutes", 15)
        self.relock.configure(text=f"Locks again after {mins} min idle" if mins else "Auto-lock is off")
        self.after(50, self.entry.focus_set)

    def submit(self):
        wait = self.lock_until - time.time()
        if wait > 0:
            self.err.configure(text=f"Too many attempts. Try again in {int(wait) + 1} s.")
            return
        pin = self.entry.get().strip()
        self.entry.delete(0, "end")
        if pin_matches(pin):
            self.attempts = 5
            self.err.configure(text="")
            self.app.unlock()
            return
        self.attempts -= 1
        E.record_metric("pin_failed", 1)
        if self.attempts <= 0:
            self.attempts = 5
            self.lock_until = time.time() + 60
            self.err.configure(text="Too many attempts. Input locked for 60 s.")
        else:
            self.err.configure(text=f"Wrong PIN. {self.attempts} attempt{'s' if self.attempts > 1 else ''} left.")


# ---------------------------------------------------------------- S2 Dashboard
class DashboardScreen(Screen):
    title, trace = "Dashboard", "S2 · US-01, US-02, US-08, US-12"

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="q")
        hero = Card(self)
        hero.grid(row=0, column=0, columnspan=4, sticky="ew")
        hero.grid_columnconfigure(1, weight=1)
        self.orb = tk.Canvas(hero, width=96, height=96, highlightthickness=0, bd=0)
        self.orb.grid(row=0, column=0, rowspan=4, padx=(18, 16), pady=16)
        self.state_lbl = label(hero, "Starting...", 18, "bold")
        self.state_lbl.grid(row=0, column=1, sticky="w", pady=(18, 0))
        self.hint = label(hero, "Press the mic button or Win+Shift+C to start listening", 12, color=MUTE)
        self.hint.grid(row=1, column=1, sticky="w")
        self.last_you = label(hero, "You: (nothing yet)", 13)
        self.last_you.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.last_chaos = label(hero, "CHAOS: (nothing yet)", 13, wraplength=560)
        self.last_chaos.grid(row=3, column=1, sticky="w", pady=(0, 16))
        chips = ctk.CTkFrame(hero, fg_color="transparent")
        chips.grid(row=0, column=2, rowspan=4, padx=16, sticky="ne", pady=16)
        self.mode_chip = app.chip(chips, "Type mode")
        self.mode_chip.pack(anchor="e", pady=(0, 6))
        self.persona_chip = app.chip(chips, "Persona")
        self.persona_chip.pack(anchor="e")

        tiles = [("Read my screen", "True Sight", lambda: (app.show("truesight"), app.screens["truesight"].scan())),
                 ("Start focus", "25 min Pomodoro", lambda: (app.show("productivity"), app.screens["productivity"].start_pomo())),
                 ("Take a note", "saved locally", lambda: (app.show("productivity"), app.screens["productivity"].note_in.focus_set())),
                 ("Ask AI", "on this PC, offline", lambda: (app.show("conversation"), app.screens["conversation"].entry.focus_set()))]
        for i, (title, sub, cmd) in enumerate(tiles):
            b = ctk.CTkButton(self, text=f"{title}\n{sub}", command=cmd, height=64, corner_radius=10,
                              fg_color=WIN, hover_color=ACCENT_SOFT, border_width=1, border_color=LINE,
                              text_color=INK, font=F(13, "bold"))
            b.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6), pady=12)

        self.tasks = Card(self, "Today's tasks")
        self.tasks.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 6))
        self.task_box = ctk.CTkFrame(self.tasks, fg_color="transparent")
        self.task_box.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        focus = Card(self, "Focus and reminders")
        focus.grid(row=2, column=2, columnspan=2, sticky="nsew", padx=(6, 0))
        label(focus, "Pomodoro", 11, color=MUTE).pack(fill="x", padx=16)
        self.pomo = label(focus, "Not running", 13)
        self.pomo.pack(fill="x", padx=16, pady=(0, 10))
        label(focus, "Next reminder", 11, color=MUTE).pack(fill="x", padx=16)
        self.rem = label(focus, "None set", 13)
        self.rem.pack(fill="x", padx=16, pady=(0, 14))
        self.pulse = 0

    def refresh(self):
        for w in self.task_box.winfo_children():
            w.destroy()
        todos = E.load_todos()
        open_n = sum(1 for t in todos if not t.get("done"))
        self.tasks.title.configure(text=f"Today's tasks  ·  {open_n} open")
        if not todos:
            label(self.task_box, 'No tasks yet. Say "add task ..."', 12, color=MUTE).pack(anchor="w")
        for i, t in enumerate(todos[:6]):
            self.app.todo_checkbox(self.task_box, i, t).pack(anchor="w", pady=2)
        upcoming = self.app.upcoming_reminders()
        self.rem.configure(text=f"{upcoming[0]['message']}, at {upcoming[0]['time'][11:16]}" if upcoming else "None set")
        self.mode_chip.configure(text="Voice mode" if self.e.listening else "Type mode")
        persona = E.setting_get("persona_key") or "butler"
        self.persona_chip.configure(text=f"Persona: {persona}")

    def tick(self):
        self.pomo.configure(text=self.app.pomo_text())
        state = self.e.voice_state if self.e.listening else ("busy" if self.e.busy else "idle")
        words = {"listening": "Listening...", "busy": "Working on it...", "speaking": "Speaking...",
                 "idle": "Type mode" if not self.e.listening else "Ready"}
        if self.app.locked:
            words["idle"] = "Locked"
        self.state_lbl.configure(text=words.get(state, "Ready") if self.e.mic_ok or not self.e.listening else "No microphone")
        self.draw_orb(state)

    def draw_orb(self, state):
        c = self.orb
        c.configure(bg=col(WIN))
        c.delete("all")
        live = state in ("listening", "speaking")
        ring = col(ACCENT) if live or state == "busy" else col(LINE)
        fill = col(ACCENT_SOFT) if live or state == "busy" else col(PANEL)
        c.create_oval(4, 4, 92, 92, outline=ring, width=2, fill=fill)
        self.pulse = (self.pulse + 1) % 20
        r = 14 + (6 * abs(10 - self.pulse) / 10 if live else 0)
        dot = col(ACCENT) if live or state == "busy" else col(MUTE)
        c.create_oval(48 - r, 48 - r, 48 + r, 48 + r, outline="", fill=dot)


# ---------------------------------------------------------------- S3 Conversation
class ConversationScreen(Screen):
    title, trace = "Conversation", "S3 · US-01, US-03, US-04, US-05"

    def __init__(self, app):
        super().__init__(app)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x")
        label(head, "Conversation", 20, "bold").pack(side="left")
        self.restored = label(head, "", 12, color=MUTE)
        self.restored.pack(side="left", padx=12, pady=(6, 0))
        self.log = ctk.CTkScrollableFrame(self, fg_color=WIN, border_width=1, border_color=LINE, corner_radius=10)
        self.log.pack(fill="both", expand=True, pady=10)
        self.log.grid_columnconfigure(0, weight=1)
        self.row = 0
        sugg = ctk.CTkFrame(self, fg_color="transparent")
        sugg.pack(fill="x")
        for s in ["what time is it", "system health", "my tasks", "read my notes", "open notepad"]:
            ctk.CTkButton(sugg, text=s, command=lambda s=s: self.send(s), height=26, corner_radius=13,
                          fg_color=PANEL, hover_color=ACCENT_SOFT, text_color=INK, border_width=1,
                          border_color=LINE, font=F(12), width=10).pack(side="left", padx=(0, 6))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 0))
        self.entry = ctk.CTkEntry(bar, placeholder_text="Type a command, or just speak...", height=38,
                                  font=F(13), border_color=LINE)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        button(bar, "Send", self.send, primary=True, width=80, height=38).pack(side="left", padx=(8, 0))
        self.load_history()

    def load_history(self):
        hist = E.load_history(limit=12)
        if hist:
            for m in hist:
                self.bubble(m["content"], m["role"] == "user", old=True)
            self.divider(f"Earlier messages, restored from {hist[-1]['timestamp'][:10]}")
        ctx = len(E.conversation_context) // 2
        self.restored.configure(text=f"{ctx} AI exchange{'s' if ctx != 1 else ''} restored from last session" if ctx else "")

    def divider(self, text):
        f = ctk.CTkFrame(self.log, fg_color="transparent")
        f.grid(row=self.row, column=0, sticky="ew", pady=8)
        self.row += 1
        label(f, f"——  {text}  ——", 11, color=MUTE).pack()

    def bubble(self, text, me, old=False, meta=None):
        outer = ctk.CTkFrame(self.log, fg_color="transparent")
        outer.grid(row=self.row, column=0, sticky="e" if me else "w", padx=8, pady=3)
        self.row += 1
        b = ctk.CTkFrame(outer, corner_radius=12, border_width=1,
                         fg_color=ACCENT_SOFT if me else PANEL, border_color=ACCENT if me and not old else LINE)
        b.pack(anchor="e" if me else "w")
        label(b, text, 13, color=MUTE if old else INK, wraplength=520).pack(padx=12, pady=(7, 0 if meta else 7), anchor="w")
        if meta:
            label(b, meta, 11, color=MUTE).pack(padx=12, pady=(0, 7), anchor="w")
        children = self.log.winfo_children()
        if len(children) > 220:
            children[0].destroy()
        self.after(30, lambda: self.log._parent_canvas.yview_moveto(1.0))

    def send(self, text=None):
        text = (text if text is not None else self.entry.get()).strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.app.user_said(text)
        if self.app.handle_ui_command(text):
            return
        self.e.submit(text)


# ---------------------------------------------------------------- S4 True Sight
class TrueSightScreen(Screen):
    title, trace = "True Sight", "S4 · US-06, US-07, UC-04, UC-05"

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        label(head, "True Sight", 20, "bold").pack(side="left")
        label(head, "Reads the text on your screen with Windows OCR. Nothing leaves this PC.", 12,
              color=MUTE).pack(side="left", padx=12, pady=(6, 0))
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=10)
        self.ask = ctk.CTkEntry(left, placeholder_text="Ask about the screen (optional), e.g. what does this error mean",
                                height=36, font=F(13), border_color=LINE)
        self.ask.pack(fill="x")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", pady=10)
        self.scan_btn = button(row, "Scan screen", self.scan, primary=True, width=140)
        self.scan_btn.pack(side="left")
        label(row, 'or say "read my screen"', 12, color=MUTE).pack(side="left", padx=10)
        self.progress = ctk.CTkProgressBar(left, height=6, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x")
        ans = Card(left, "Answer")
        ans.pack(fill="x", pady=10)
        self.answer = label(ans, "Press Scan screen to read what is on your screen. CHAOS hides this window "
                                 "for a moment so it reads the app behind it.", 13, wraplength=520)
        self.answer.pack(fill="x", padx=16)
        self.timing = label(ans, "", 11, color=MUTE)
        self.timing.pack(fill="x", padx=16, pady=(4, 14))
        ocr = Card(left)
        ocr.pack(fill="both", expand=True)
        top = ctk.CTkFrame(ocr, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(10, 4))
        label(top, "Recognised text (OCR)", 14, "bold").pack(side="left")
        self.toggle_btn = link(top, "Show", self.toggle_ocr)
        self.toggle_btn.pack(side="right")
        self.ocr_text = ctk.CTkTextbox(ocr, height=120, font=ctk.CTkFont(family="Consolas", size=12),
                                       fg_color=PANEL, text_color=MUTE)
        self.ocr_text.insert("end", "Nothing read yet.")
        self.ocr_text.configure(state="disabled")

        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", pady=10)
        w = Card(right, "Proactive watcher")
        w.pack(fill="x")
        self.watch = switch(w, "Off", self.toggle_watch, on=bool(E.screen_watcher_enabled))
        self.watch.pack(anchor="w", padx=16)
        label(w, "Checks every 10 s when the screen changes. Spoken alerts at most every 30 s.", 12,
              color=MUTE, wraplength=300).pack(fill="x", padx=16, pady=(6, 14))
        p = Card(right, "Privacy")
        p.pack(fill="x", pady=10)
        label(p, "The screenshot is deleted straight after reading. OCR and the AI run on this PC only.",
              12, color=MUTE, wraplength=300).pack(fill="x", padx=16, pady=(0, 14))
        a = Card(right, "Last alert")
        a.pack(fill="x")
        self.alert = label(a, "None this session", 12, color=MUTE, wraplength=300)
        self.alert.pack(fill="x", padx=16, pady=(0, 14))
        self.ocr_shown = False

    def toggle_ocr(self):
        self.ocr_shown = not self.ocr_shown
        if self.ocr_shown:
            self.ocr_text.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        else:
            self.ocr_text.pack_forget()
        self.toggle_btn.configure(text="Hide" if self.ocr_shown else "Show")

    def toggle_watch(self):
        on = bool(self.watch.get())
        msg = E.start_screen_watcher() if on else E.stop_screen_watcher()
        self.watch.configure(text="On" if on else "Off")
        print(f"[True Sight] {msg}")

    def scan(self):
        if str(self.scan_btn.cget("state")) == "disabled":
            return
        question = self.ask.get().strip() or "Describe what is on this screen."
        self.scan_btn.configure(state="disabled")
        self.answer.configure(text="Reading the screen with Windows OCR...", text_color=MUTE)
        self.timing.configure(text="")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.app.withdraw_for_capture()
        threading.Thread(target=self._scan_worker, args=(question,), daemon=True).start()

    def _scan_worker(self, question):
        time.sleep(0.4)  # let the window finish hiding
        start = time.time()
        try:
            text = E.ocr_screen()
        except Exception as e:
            self.app.post("scan_done", (None, f"True Sight couldn't read the screen: {e}", None, ""))
            return
        finally:
            self.app.post("restore_window", None)
        ocr_time = time.time() - start
        if not text:
            self.app.post("scan_done", ("", "I couldn't find any readable text on your screen.", None, ""))
            return
        self.app.post("scan_stage", "Asking the local AI about the screen text...")
        try:
            ans = E.ask_about_screen_text(text, f"{question} Answer in at most 3 short sentences, suitable for speaking aloud.",
                                          E.TRUE_SIGHT_MAX_REPLY_TOKENS)
        except E.requests.exceptions.ConnectionError:
            self.app.post("scan_done", (text, "Ollama is not running. Please start Ollama first.", None, ""))
            self.app.post("ollama_down", None)
            return
        except Exception as e:
            self.app.post("scan_done", (text, f"True Sight couldn't analyse the screen: {e}", None, ""))
            return
        total = time.time() - start
        E.record_metric("true_sight_s", total)
        self.app.post("scan_done", (text, ans, total, f"OCR {ocr_time:.1f} s + analysis {total - ocr_time:.1f} s"))
        if E.voice_mode:
            threading.Thread(target=lambda: E.speak(ans, allow_interrupt=False), daemon=True).start()

    def done(self, text, ans, total, detail):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.scan_btn.configure(state="normal")
        self.answer.configure(text=ans, text_color=INK)
        self.timing.configure(text=f"{total:.1f} s total: {detail} (target 5 s)" if total else "")
        if text is not None:
            self.ocr_text.configure(state="normal")
            self.ocr_text.delete("1.0", "end")
            self.ocr_text.insert("end", text or "(no text found)")
            self.ocr_text.configure(state="disabled")


# ---------------------------------------------------------------- S5 Productivity
class ProductivityScreen(Screen):
    title, trace = "Productivity", "S5 · US-09, US-11, US-12, US-13"

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure((0, 1, 2), weight=1, uniform="p")
        self.grid_rowconfigure(1, weight=1)
        label(self, "Productivity", 20, "bold").grid(row=0, column=0, sticky="w", pady=(0, 10))

        pomo = Card(self, "Pomodoro")
        pomo.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.ring = tk.Canvas(pomo, width=160, height=160, highlightthickness=0, bd=0)
        self.ring.pack(pady=6)
        self.phase = label(pomo, "", 12, color=MUTE)
        self.phase.pack()
        row = ctk.CTkFrame(pomo, fg_color="transparent")
        row.pack(pady=10)
        self.pomo_btn = button(row, "Start", self.start_pomo, primary=True, width=90)
        self.pomo_btn.pack(side="left", padx=4)
        button(row, "Stop", self.stop_pomo, width=90).pack(side="left", padx=4)
        label(pomo, "25 min focus, 5 min break. Breaks are announced aloud.\nSay \"start pomodoro\".",
              11, color=MUTE).pack(padx=16, pady=(0, 14))

        todo = Card(self, "To-do")
        todo.grid(row=1, column=1, sticky="nsew", padx=6)
        self.todo_card = todo
        self.todo_box = ctk.CTkScrollableFrame(todo, fg_color="transparent", height=220)
        self.todo_box.pack(fill="both", expand=True, padx=8)
        add = ctk.CTkFrame(todo, fg_color="transparent")
        add.pack(fill="x", padx=16, pady=8)
        self.todo_in = ctk.CTkEntry(add, placeholder_text="Add a task...", height=32, border_color=LINE)
        self.todo_in.pack(side="left", fill="x", expand=True)
        self.todo_in.bind("<Return>", lambda e: self.add_todo())
        button(add, "Add", self.add_todo, width=60).pack(side="left", padx=(6, 0))
        foot = ctk.CTkFrame(todo, fg_color="transparent")
        foot.pack(fill="x", padx=12, pady=(0, 10))
        link(foot, "Clear completed", self.clear_done).pack(side="left")
        label(foot, 'Say "complete task 2"', 11, color=MUTE).pack(side="right")

        notes = Card(self, "Notes")
        notes.grid(row=1, column=2, sticky="nsew", padx=(6, 0))
        self.note_box = ctk.CTkScrollableFrame(notes, fg_color="transparent", height=120)
        self.note_box.pack(fill="x", padx=8)
        addn = ctk.CTkFrame(notes, fg_color="transparent")
        addn.pack(fill="x", padx=16, pady=8)
        self.note_in = ctk.CTkEntry(addn, placeholder_text="Write a note...", height=32, border_color=LINE)
        self.note_in.pack(side="left", fill="x", expand=True)
        self.note_in.bind("<Return>", lambda e: self.add_note())
        button(addn, "Save", self.add_note, width=60).pack(side="left", padx=(6, 0))
        label(notes, "Reminders", 14, "bold").pack(fill="x", padx=16, pady=(6, 2))
        self.rem_box = ctk.CTkFrame(notes, fg_color="transparent")
        self.rem_box.pack(fill="x", padx=16)
        addr = ctk.CTkFrame(notes, fg_color="transparent")
        addr.pack(fill="x", padx=16, pady=(6, 14))
        self.rem_in = ctk.CTkEntry(addr, placeholder_text="Remind me to...", height=32, border_color=LINE)
        self.rem_in.pack(side="left", fill="x", expand=True)
        self.rem_min = ctk.CTkOptionMenu(addr, values=["5 min", "15 min", "30 min", "60 min"], width=80,
                                         fg_color=PANEL, button_color=PANEL, text_color=INK, button_hover_color=LINE)
        self.rem_min.set("15 min")
        self.rem_min.pack(side="left", padx=6)
        button(addr, "Set", self.add_reminder, width=50).pack(side="left")

    def start_pomo(self):
        print(f"[Pomodoro] {E.start_pomodoro()}")
        self.tick()

    def stop_pomo(self):
        print(f"[Pomodoro] {E.stop_pomodoro()}")
        self.tick()

    def tick(self):
        c = self.ring
        c.configure(bg=col(WIN))
        c.delete("all")
        c.create_oval(14, 14, 146, 146, outline=col(LINE), width=10)
        left, frac, phase = self.app.pomo_state()
        if frac > 0:
            c.create_arc(14, 14, 146, 146, start=90, extent=-360 * frac, style="arc", outline=col(ACCENT), width=10)
        c.create_text(80, 80, text=fmt_mmss(left) if E.pomodoro_active else "25:00", fill=col(INK),
                      font=(FONT, 26, "bold"))
        self.phase.configure(text=phase)
        self.pomo_btn.configure(state="disabled" if E.pomodoro_active else "normal")

    def refresh(self):
        for w in self.todo_box.winfo_children():
            w.destroy()
        todos = E.load_todos()
        open_n = sum(1 for t in todos if not t.get("done"))
        self.todo_card.title.configure(text=f"To-do  ·  {open_n} open")
        if not todos:
            label(self.todo_box, 'No tasks yet. Add one below or say "add task ..."', 12, color=MUTE,
                  wraplength=240).pack(anchor="w", padx=8, pady=6)
        for i, t in enumerate(todos):
            self.app.todo_checkbox(self.todo_box, i, t).pack(anchor="w", padx=8, pady=3)
        for w in self.note_box.winfo_children():
            w.destroy()
        notes = E.db_read("SELECT content, timestamp FROM notes ORDER BY id DESC LIMIT 20")
        if not notes:
            label(self.note_box, 'No notes yet. Say "take a note ..." or write one below.', 12, color=MUTE,
                  wraplength=240).pack(anchor="w", padx=8, pady=6)
        for content, ts in notes:
            label(self.note_box, content, 13, wraplength=250).pack(anchor="w", padx=8)
            label(self.note_box, ts.replace("T", " ")[:16], 11, color=MUTE).pack(anchor="w", padx=8, pady=(0, 6))
        for w in self.rem_box.winfo_children():
            w.destroy()
        upcoming = self.app.upcoming_reminders()
        if not upcoming:
            label(self.rem_box, "No upcoming reminders.", 12, color=MUTE).pack(anchor="w")
        for r in upcoming[:4]:
            label(self.rem_box, f"{r['message']}  ·  {r['time'][11:16]}", 12).pack(anchor="w")

    def add_todo(self):
        v = self.todo_in.get().strip()
        if v:
            E.add_todo(v)
            self.todo_in.delete(0, "end")
            self.app.refresh_all()

    def clear_done(self):
        E.clear_todos()
        self.app.refresh_all()

    def add_note(self):
        v = self.note_in.get().strip()
        if v:
            E.save_note(v)
            self.note_in.delete(0, "end")
            self.app.toast("Note saved")
            self.refresh()

    def add_reminder(self):
        v = self.rem_in.get().strip()
        if v:
            minutes = int(self.rem_min.get().split()[0])
            E.set_reminder(v, minutes)
            self.rem_in.delete(0, "end")
            self.app.toast(f"Reminder set for {minutes} minutes from now")
            self.app.refresh_all()


# ---------------------------------------------------------------- S6 Memory and History
class MemoryScreen(Screen):
    title, trace = "Memory and History", "S6 · US-04, US-10, US-16, UC-20"

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure((0, 1), weight=1, uniform="m")
        self.grid_rowconfigure(1, weight=1)
        label(self, "Memory and History", 20, "bold").grid(row=0, column=0, sticky="w", pady=(0, 10))
        facts = Card(self, "Remembered facts")
        facts.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.fact_box = ctk.CTkScrollableFrame(facts, fg_color="transparent")
        self.fact_box.pack(fill="both", expand=True, padx=8)
        label(facts, 'Say "remember my X is Y" or "forget my X"', 11, color=MUTE).pack(fill="x", padx=16, pady=(4, 12))
        hist = Card(self, "Conversation history  ·  last 30 messages")
        hist.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self.hist_box = ctk.CTkScrollableFrame(hist, fg_color="transparent")
        self.hist_box.pack(fill="both", expand=True, padx=8)
        foot = ctk.CTkFrame(hist, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(6, 14))
        self.clear_btn = button(foot, "Clear history", self.ask_clear, danger=True, width=130)
        self.clear_btn.pack(anchor="w")
        self.confirm = ctk.CTkFrame(foot, fg_color=BAD_SOFT, border_width=1, border_color=BAD, corner_radius=8)
        label(self.confirm, "Clear all conversation history?", 13, "bold").pack(anchor="w", padx=12, pady=(10, 0))
        label(self.confirm, "Notes, facts and tasks are kept. This cannot be undone.", 12).pack(anchor="w", padx=12)
        r = ctk.CTkFrame(self.confirm, fg_color="transparent")
        r.pack(anchor="w", padx=12, pady=10)
        button(r, "Cancel", lambda: self.confirm.pack_forget(), width=80).pack(side="left")
        button(r, "Clear history", self.do_clear, danger=True, width=120).pack(side="left", padx=8)

    def refresh(self):
        for w in self.fact_box.winfo_children():
            w.destroy()
        mem = E.load_memory()
        if not mem:
            label(self.fact_box, "Nothing remembered yet.", 12, color=MUTE).pack(anchor="w", padx=8, pady=6)
        for key, v in mem.items():
            row = ctk.CTkFrame(self.fact_box, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            label(row, key, 13, "bold", width=130).pack(side="left")
            label(row, str(v.get("value", "")), 13, wraplength=200).pack(side="left", fill="x", expand=True)
            link(row, "Forget", lambda k=key: self.forget(k)).pack(side="right")
        for w in self.hist_box.winfo_children():
            w.destroy()
        hist = list(reversed(E.load_history(limit=30)))
        if not hist:
            label(self.hist_box, "No conversation history yet.", 12, color=MUTE).pack(anchor="w", padx=8, pady=6)
        for m in hist:
            who = "You" if m["role"] == "user" else "CHAOS"
            label(self.hist_box, f"{m['timestamp'][11:16]}  {who}: {m['content'][:160]}", 12,
                  wraplength=380).pack(anchor="w", padx=8, pady=1)
        self.clear_btn.configure(state="normal" if hist else "disabled")

    def forget(self, key):
        E.forget_fact(key)
        self.app.toast(f"Forgot {key}")
        self.refresh()

    def ask_clear(self):
        self.confirm.pack(fill="x", pady=(10, 0))

    def do_clear(self):
        E.db_write("DELETE FROM conversations")
        E.conversation_context.clear()
        E.store_save("llm_context", [])
        self.confirm.pack_forget()
        self.app.toast("Conversation history cleared")
        self.refresh()


# ---------------------------------------------------------------- S7 Settings
class SettingsScreen(Screen):
    title, trace = "Settings (Device Owner)", "S7 · US-14, US-15, US-18"
    PERSONAS = ["butler", "jarvis", "friday", "alfred", "professional", "casual", "friendly", "wizard",
                "narrator", "coach", "egirl", "pirate", "female", "male"]
    AUTOLOCK = {"5 minutes": 5, "15 minutes": 15, "30 minutes": 30, "Never": 0}

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        label(self, "Settings", 20, "bold").grid(row=0, column=0, sticky="w", pady=(0, 10))
        prefs = Card(self, "Preferences")
        prefs.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        g = ctk.CTkFrame(prefs, fg_color="transparent")
        g.pack(fill="x", padx=16, pady=(0, 14))
        g.grid_columnconfigure(1, weight=1)

        def row(r, text):
            label(g, text, 13).grid(row=r, column=0, sticky="w", pady=7, padx=(0, 16))

        menu_style = dict(fg_color=PANEL, button_color=PANEL, text_color=INK, button_hover_color=LINE,
                          dropdown_font=F(12), font=F(13), width=230)
        row(0, "Voice persona")
        pr = ctk.CTkFrame(g, fg_color="transparent")
        pr.grid(row=0, column=1, sticky="w")
        self.persona = ctk.CTkOptionMenu(pr, values=self.PERSONAS, command=self.set_persona, **menu_style)
        self.persona.pack(side="left")
        link(pr, "Play sample", self.sample).pack(side="left", padx=8)
        row(1, "Voice output")
        self.voice_out = switch(g, "Speak replies aloud", self.set_voice_out, on=E.voice_mode)
        self.voice_out.grid(row=1, column=1, sticky="w")
        row(2, "Input mode")
        self.mode = ctk.CTkSegmentedButton(g, values=["Voice", "Type"], command=self.set_mode,
                                           selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, font=F(13))
        self.mode.grid(row=2, column=1, sticky="w")
        row(3, "Wake word")
        self.wake = switch(g, 'Listen for "Hey CHAOS"', self.set_wake, on=bool(getattr(E, "wake_word_active", False)))
        self.wake.grid(row=3, column=1, sticky="w")
        row(4, "AI model")
        self.model = ctk.CTkOptionMenu(g, values=[E.ollama_model], command=self.set_model, **menu_style)
        self.model.grid(row=4, column=1, sticky="w")
        row(5, "AI brain")
        self.brain = switch(g, "Answer open questions with the local model", self.set_brain, on=E.ollama_enabled)
        self.brain.grid(row=5, column=1, sticky="w")
        row(6, "Urdu mode")
        self.urdu = switch(g, "Reply in Roman Urdu", self.set_urdu, on=E.urdu_mode)
        self.urdu.grid(row=6, column=1, sticky="w")
        label(g, "Changes save instantly to chaos_data.db", 11, color=MUTE).grid(row=7, column=1, sticky="w", pady=(8, 0))

        sec = Card(self, "Privacy and security")
        sec.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        s = ctk.CTkFrame(sec, fg_color="transparent")
        s.pack(fill="x", padx=16, pady=(0, 14))
        label(s, "Offline mode", 13, "bold").pack(anchor="w")
        off = switch(s, "On, locked. Changed only in the config file.", None, on=E.OFFLINE_MODE)
        off.configure(state="disabled")
        off.pack(anchor="w", pady=(2, 10))
        label(s, "PIN lock", 13, "bold").pack(anchor="w")
        self.pin_state = label(s, "", 12, color=MUTE)
        self.pin_state.pack(anchor="w")
        pr2 = ctk.CTkFrame(s, fg_color="transparent")
        pr2.pack(anchor="w", pady=(2, 10))
        self.pin_btn = link(pr2, "Set a PIN", self.set_pin)
        self.pin_btn.pack(side="left")
        self.pin_remove = link(pr2, "Remove PIN", self.remove_pin)
        self.pin_remove.pack(side="left", padx=8)
        label(s, "Auto-lock", 13, "bold").pack(anchor="w")
        self.autolock = ctk.CTkOptionMenu(s, values=list(self.AUTOLOCK), command=self.set_autolock,
                                          **{**menu_style, "width": 160})
        self.autolock.pack(anchor="w", pady=(2, 10))
        label(s, "Stored data", 13, "bold").pack(anchor="w")
        self.data_lbl = label(s, "", 11, color=MUTE, wraplength=280)
        self.data_lbl.pack(anchor="w")
        link(s, "Export history to a file", self.export).pack(anchor="w")
        link(s, "Open data folder", lambda: os.startfile(E.APP_DIR)).pack(anchor="w")
        link(s, "Run system check", lambda: app.show("setup")).pack(anchor="w")

    def on_show(self):
        key = E.setting_get("persona_key") or "butler"
        if key not in self.PERSONAS:
            self.persona.configure(values=self.PERSONAS + [key])
        self.persona.set(key)
        self.mode.set("Voice" if self.e.listening else "Type")
        models = ollama_models() or [E.ollama_model]
        self.model.configure(values=models)
        self.model.set(next((m for m in models if m.split(":")[0] == E.ollama_model.split(":")[0]), E.ollama_model))
        has_pin = bool(E.setting_get("pin_hash"))
        self.pin_state.configure(text="On" if has_pin else "Off: anyone at this PC can open CHAOS")
        self.pin_btn.configure(text="Change PIN" if has_pin else "Set a PIN")
        self.pin_remove.configure(state="normal" if has_pin else "disabled")
        mins = E.setting_get("auto_lock_minutes", 15)
        self.autolock.set(next((k for k, v in self.AUTOLOCK.items() if v == mins), "15 minutes"))
        try:
            size = os.path.getsize(E.DB_PATH) / 1024
        except OSError:
            size = 0
        self.data_lbl.configure(text=f"{E.DB_PATH}  ({size:.0f} KB)")

    def set_persona(self, key):
        E.current_voice = E.VOICE_MAP.get(key, E.current_voice)
        E.setting_set("persona_key", key)
        E.save_preferences()
        self.app.refresh_all()
        self.sample()

    def sample(self):
        key = E.setting_get("persona_key") or "butler"
        threading.Thread(target=lambda: E.speak(f"Voice changed to {key}. How do I sound?", allow_interrupt=False,
                                                force_voice=True), daemon=True).start()

    def set_voice_out(self):
        E.voice_mode = bool(self.voice_out.get())
        E.save_preferences()

    def set_mode(self, value):
        self.app.set_listening(value == "Voice")

    def set_wake(self):
        print(E.start_wake_word_detection() if self.wake.get() else E.stop_wake_word_detection())

    def set_model(self, name):
        E.ollama_model = name
        E.setting_set("ollama_model", name)
        self.app.update_bar()
        threading.Thread(target=E.warmup_ollama, daemon=True).start()

    def set_brain(self):
        E.ollama_enabled = bool(self.brain.get())
        E.save_preferences()

    def set_urdu(self):
        E.urdu_mode = bool(self.urdu.get())
        E.save_preferences()

    def set_autolock(self, value):
        E.setting_set("auto_lock_minutes", self.AUTOLOCK[value])

    def set_pin(self):
        PinDialog(self.app, change=bool(E.setting_get("pin_hash")), on_done=self.on_show)

    def remove_pin(self):
        PinDialog(self.app, change=True, remove=True, on_done=self.on_show)

    def export(self):
        folder = os.path.join(E.APP_DIR, "workspace")
        os.makedirs(folder, exist_ok=True)
        ok, result = E.export_conversation(folder)
        if ok:
            self.app.toast(f"Exported to {result}")
            os.startfile(folder)
        else:
            self.app.toast(result)


class PinDialog(ctk.CTkToplevel):
    def __init__(self, app, change=False, remove=False, on_done=None):
        super().__init__(app)
        self.app, self.change, self.remove, self.on_done = app, change, remove, on_done
        self.title("Remove PIN" if remove else ("Change PIN" if change else "Set a PIN"))
        self.geometry("340x320")
        self.resizable(False, False)
        self.configure(fg_color=WIN)
        self.transient(app)
        self.after(100, self.grab_set)
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=24, pady=20)
        self.cur = self.new = self.again = None
        if change:
            label(f, "Current PIN", 12, color=MUTE).pack(anchor="w")
            self.cur = ctk.CTkEntry(f, show="•", border_color=LINE)
            self.cur.pack(fill="x", pady=(0, 10))
        if not remove:
            label(f, "New PIN (4 to 6 digits)", 12, color=MUTE).pack(anchor="w")
            self.new = ctk.CTkEntry(f, show="•", border_color=LINE)
            self.new.pack(fill="x", pady=(0, 10))
            label(f, "New PIN again", 12, color=MUTE).pack(anchor="w")
            self.again = ctk.CTkEntry(f, show="•", border_color=LINE)
            self.again.pack(fill="x")
        self.err = label(f, "", 12, color=BAD)
        self.err.pack(anchor="w", pady=8)
        button(f, "Remove PIN" if remove else "Save PIN", self.save, primary=not remove, danger=remove).pack(fill="x")

    def save(self):
        if self.cur is not None and not pin_matches(self.cur.get()):
            self.err.configure(text="The current PIN is wrong.")
            return
        if self.remove:
            E.db_write("DELETE FROM settings WHERE key = 'pin_hash'")
            self.app.toast("PIN removed")
        else:
            new = self.new.get()
            if not (new.isdigit() and 4 <= len(new) <= 6):
                self.err.configure(text="Use 4 to 6 digits.")
                return
            if new != self.again.get():
                self.err.configure(text="The two PINs don't match.")
                return
            E.setting_set("pin_hash", pin_record(new))
            self.app.toast("PIN saved. CHAOS will ask for it at start and after auto-lock.")
        if self.on_done:
            self.on_done()
        self.destroy()


# ---------------------------------------------------------------- S8 Performance
class PerformanceScreen(Screen):
    title, trace = "Performance (Device Owner)", "S8 · US-17, UC-21"

    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure((0, 1, 2), weight=1, uniform="k")
        self.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        label(head, "Performance", 20, "bold").pack(side="left")
        label(head, "measured live on this PC; targets from Deliverable 3", 12, color=MUTE).pack(side="left", padx=12, pady=(6, 0))
        self.kpis = []
        for i, (name, target) in enumerate([("CHAOS RAM", "limit 1024 MB (NFR-04)"),
                                            ("Command response, last 20 avg", "target 2 s (NFR-01)"),
                                            ("True Sight, last scan", "target 5 s (NFR-02)")]):
            c = Card(self)
            c.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            label(c, name, 11, color=MUTE).pack(fill="x", padx=16, pady=(12, 0))
            v = label(c, "-", 26, "bold")
            v.pack(fill="x", padx=16)
            bar = ctk.CTkProgressBar(c, height=8, progress_color=ACCENT)
            bar.set(0)
            bar.pack(fill="x", padx=16, pady=4)
            t = label(c, target, 11, color=MUTE)
            t.pack(fill="x", padx=16, pady=(0, 12))
            self.kpis.append((v, bar, t, target))
        chart = Card(self, "Response times, last 20 commands")
        chart.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 6), pady=12)
        self.chart = tk.Canvas(chart, height=190, highlightthickness=0, bd=0)
        self.chart.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        side = ctk.CTkFrame(self, fg_color="transparent")
        side.grid(row=2, column=2, sticky="nsew", padx=(6, 0), pady=12)
        pc = Card(side, "PC health")
        pc.pack(fill="x")
        self.pc = label(pc, "", 12)
        self.pc.pack(fill="x", padx=16, pady=(0, 14))
        logc = Card(side, "Activity log")
        logc.pack(fill="both", expand=True, pady=(12, 0))
        self.log = ctk.CTkTextbox(logc, font=ctk.CTkFont(family="Consolas", size=11), fg_color=PANEL, text_color=MUTE)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def refresh(self):
        try:
            ram = E.get_chaos_memory_mb()
        except Exception:
            ram = 0
        times = [v for (v,) in E.db_read("SELECT value FROM metrics WHERE name = 'response_time_s' ORDER BY id DESC LIMIT 20")][::-1]
        ts = E.db_read("SELECT value FROM metrics WHERE name = 'true_sight_s' ORDER BY id DESC LIMIT 1")
        avg = sum(times) / len(times) if times else None
        vals = [(f"{ram:.0f} MB", ram / 1024, ram <= 1024),
                (f"{avg:.2f} s" if avg is not None else "no data yet", (avg or 0) / 2, avg is None or avg <= 2),
                (f"{ts[0][0]:.1f} s" if ts else "no scan yet", (ts[0][0] / 5) if ts else 0, not ts or ts[0][0] <= 5)]
        for (v, bar, t, target), (text, frac, ok) in zip(self.kpis, vals):
            v.configure(text=text)
            bar.set(min(1, frac))
            bar.configure(progress_color=ACCENT if ok else BAD)
            t.configure(text=f"{target}  ·  {'within' if ok else 'over'}", text_color=MUTE if ok else BAD)
        self.draw_chart(times)
        try:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
            bat = psutil.sensors_battery()
            lines = [f"CPU            {psutil.cpu_percent(interval=None):.0f}%",
                     f"RAM            {mem.percent:.0f}% of {mem.total / 1e9:.1f} GB",
                     f"Disk {os.environ.get('SystemDrive', 'C:')}        {disk.percent:.0f}% used"]
            if bat:
                lines.append(f"Battery        {bat.percent:.0f}%{', charging' if bat.power_plugged else ''}")
            self.pc.configure(text="\n".join(lines), font=ctk.CTkFont(family="Consolas", size=12))
        except Exception as e:
            self.pc.configure(text=f"Unavailable: {e}")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(list(LOG_LINES)[-120:]))
        self.log.see("end")
        self.log.configure(state="disabled")

    def draw_chart(self, data):
        c = self.chart
        c.configure(bg=col(WIN))
        c.delete("all")
        c.update_idletasks()
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 160)
        left, bottom, top = 34, 22, 10
        top_val = max(2.5, max(data) * 1.15 if data else 2.5)
        y = lambda v: top + (h - top - bottom) * (1 - v / top_val)
        for v in range(0, int(top_val) + 1):
            c.create_line(left, y(v), w, y(v), fill=col(LINE))
            c.create_text(left - 6, y(v), text=f"{v} s", anchor="e", fill=col(MUTE), font=(FONT, 9))
        if not data:
            c.create_text((left + w) / 2, h / 2, text="No commands timed yet. Give CHAOS a command.",
                          fill=col(MUTE), font=(FONT, 10))
        bw = (w - left - 6) / 20
        for i, v in enumerate(data):
            x = left + 4 + i * bw
            c.create_rectangle(x, y(v), x + bw - 4, y(0), outline="", fill=col(ACCENT if v <= 2 else BAD))
        c.create_line(left, y(2), w, y(2), fill=col(BAD), dash=(5, 4))
        c.create_text(w - 2, y(2) - 8, text="2 s target", anchor="e", fill=col(MUTE), font=(FONT, 9))


# ---------------------------------------------------------------- System check / first run
class SetupScreen(Screen):
    title, trace = "System check", "First-run setup · Ollama, AI model, speech model, microphone, voices"

    def __init__(self, app):
        super().__init__(app)
        label(self, "System check", 20, "bold").pack(anchor="w")
        label(self, "CHAOS runs entirely on this PC. These parts must be in place; downloads happen only when you press a button.",
              12, color=MUTE, wraplength=760).pack(anchor="w", pady=(2, 12))
        self.card = Card(self)
        self.card.pack(fill="x")
        self.rows = {}
        for key, name in [("ollama", "Ollama runtime"), ("model", f"AI model ({DEFAULT_MODEL}, 2.0 GB)"),
                          ("whisper", "Speech model (Whisper base)"), ("mic", "Microphone"),
                          ("voices", "Windows voices"), ("ocr", "Windows OCR (True Sight)")]:
            r = ctk.CTkFrame(self.card, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=8)
            dot = label(r, "●", 16, color=MUTE, width=24)
            dot.pack(side="left")
            label(r, name, 13, "bold", width=260).pack(side="left")
            status = label(r, "Checking...", 12, color=MUTE)
            status.pack(side="left", fill="x", expand=True)
            act = ctk.CTkFrame(r, fg_color="transparent", width=1, height=1)
            act.pack(side="right")
            self.rows[key] = (dot, status, act)
        self.pull_bar = ctk.CTkProgressBar(self.card, progress_color=ACCENT)
        self.pull_bar.set(0)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", pady=14)
        button(bottom, "Check again", self.run_checks, width=120).pack(side="left")
        button(bottom, "Continue to CHAOS", self.finish, primary=True, width=180).pack(side="right")
        self.pulling = False

    def on_show(self):
        self.run_checks()

    def set_row(self, key, ok, text, actions=()):
        dot, status, act = self.rows[key]
        dot.configure(text_color=GOOD if ok is True else (WARN if ok is None else BAD))
        status.configure(text=text)
        for w in act.winfo_children():
            w.destroy()
        for name, cmd in actions:
            button(act, name, cmd, primary=True, width=10).pack(side="left", padx=4)

    def run_checks(self):
        for k in self.rows:
            self.set_row(k, None, "Checking...")
        threading.Thread(target=self._checks, daemon=True).start()

    def _checks(self):
        res = {}
        models = ollama_models()
        if models is None and find_ollama_exe():
            res["ollama"] = (None, "Installed but not running. Starting it...", ())
            self.app.post("setup_rows", dict(res))
            started = start_ollama()
            models = ollama_models() if started else None
        if models is not None:
            res["ollama"] = (True, "Running on localhost:11434", ())
            if has_model(models, DEFAULT_MODEL):
                res["model"] = (True, "Installed", ())
            elif models:
                res["model"] = (None, f"Not installed. CHAOS will use {models[0]} for now.", (("Download", self.pull),))
            else:
                res["model"] = (False, "No AI model installed yet.", (("Download", self.pull),))
        else:
            exe = find_ollama_exe()
            res["ollama"] = (False, "Installed but could not be started." if exe else "Not installed. Get it from ollama.com.",
                             (("Start", self.run_checks),) if exe else (("Get Ollama", lambda: webbrowser.open("https://ollama.com/download")),))
            res["model"] = (False, "Needs Ollama first.", ())
        try:
            E.get_whisper_model()
            res["whisper"] = (True, "Ready", ())
        except Exception as e:
            res["whisper"] = (False, str(e)[:120], ())
        try:
            import speech_recognition as sr
            names = sr.Microphone.list_microphone_names()
            res["mic"] = (True, f"Ready ({len(names)} audio devices detected)", ()) if names else (False, "No microphone found. Type mode still works.", ())
        except Exception as e:
            res["mic"] = (False, f"Not available: {e}"[:120], ())
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            voices = [t.GetDescription() for t in comtypes.client.CreateObject("SAPI.SpVoice").GetVoices()]
            res["voices"] = (True, ", ".join(v.split(" - ")[0].replace("Microsoft ", "") for v in voices)[:120], ()) if voices \
                else (False, "No voices installed. Replies will be text only.", ())
        except Exception as e:
            res["voices"] = (False, f"Not available: {e}"[:120], ())
        res["ocr"] = (True, "Built into Windows 10/11", ()) if os.path.isfile(E.TRUE_SIGHT_OCR_SCRIPT) \
            else (False, "OCR helper script missing from the install.", ())
        self.app.post("setup_rows", res)
        if models is not None:
            self.app.post("ollama_ok", models)

    def show_rows(self, res):
        for k, (ok, text, actions) in res.items():
            self.set_row(k, ok, text, actions)

    def pull(self):
        if self.pulling:
            return
        self.pulling = True
        self.pull_bar.pack(fill="x", padx=16, pady=(0, 14))
        self.set_row("model", None, "Starting download...")
        threading.Thread(target=self._pull, daemon=True).start()

    def _pull(self):
        try:
            with E.requests.post(OLLAMA + "/api/pull", json={"name": DEFAULT_MODEL, "stream": True},
                                 stream=True, timeout=(5, None)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    total, done = d.get("total"), d.get("completed")
                    frac = done / total if total and done else None
                    text = d.get("status", "")
                    if frac is not None:
                        text = f"Downloading: {done / 1e9:.2f} of {total / 1e9:.2f} GB"
                    self.app.post("pull_progress", (frac, text))
            E.ollama_model = DEFAULT_MODEL
            E.setting_set("ollama_model", DEFAULT_MODEL)
            self.app.post("pull_progress", (1.0, "Installed"))
            self.app.post("pull_done", True)
            E.warmup_ollama()
        except Exception as e:
            self.app.post("pull_progress", (0, f"Download failed: {e}"[:140]))
            self.app.post("pull_done", False)
        finally:
            self.pulling = False

    def finish(self):
        E.setting_set("setup_seen", True)
        self.app.show("dashboard")


# ---------------------------------------------------------------- main window
class ChaosApp(ctk.CTk):
    NAV = [("dashboard", "Dashboard", "Ctrl+1"), ("conversation", "Conversation", "Ctrl+2"),
           ("truesight", "True Sight", "Ctrl+3"), ("productivity", "Productivity", "Ctrl+4"),
           ("memory", "Memory", "Ctrl+5"), (None, "Device Owner", None),
           ("settings", "Settings", "Ctrl+6"), ("performance", "Performance", "Ctrl+7"), (None, None, None),
           ("setup", "System check", None)]

    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("CHAOS Control Panel")
        self.geometry("1120x720")
        self.minsize(960, 620)
        try:
            self.iconbitmap(resource("chaos_new.ico"))
        except Exception:
            pass
        self.uiq = queue.Queue()
        self.locked = False
        self.current = None
        self.last_activity = time.time()
        self.toast_job = None

        # Engine state restored from the database before anything is drawn
        E.get_db()
        E.load_preferences()
        saved_model = E.setting_get("ollama_model")
        if saved_model:
            E.ollama_model = saved_model
        self.engine = EngineBridge(self)

        self._build_bar()
        self.body_host = ctk.CTkFrame(self, fg_color=WIN, corner_radius=0)
        self.body_host.pack(fill="both", expand=True)
        self.body = ctk.CTkFrame(self.body_host, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self._build_rail()
        right = ctk.CTkFrame(self.body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        self.banner_area = ctk.CTkFrame(right, fg_color="transparent", height=0)
        self.banner_area.pack(fill="x", padx=22, pady=(14, 0))
        self.content = ctk.CTkFrame(right, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=22, pady=(8, 18))
        self.toast_lbl = ctk.CTkLabel(self, text="", fg_color=INK, text_color=WIN, corner_radius=8, font=F(12))
        self.banners = {}

        self.screens = {"dashboard": DashboardScreen(self), "conversation": ConversationScreen(self),
                        "truesight": TrueSightScreen(self), "productivity": ProductivityScreen(self),
                        "memory": MemoryScreen(self), "settings": SettingsScreen(self),
                        "performance": PerformanceScreen(self), "setup": SetupScreen(self)}
        self.unlock_screen = UnlockScreen(self)

        for i, key in enumerate(["dashboard", "conversation", "truesight", "productivity", "memory", "settings", "performance"], 1):
            self.bind_all(f"<Control-Key-{i}>", lambda e, k=key: None if self.locked else self.show(k))
        for seq in ("<Any-KeyPress>", "<Motion>", "<Button>"):
            self.bind_all(seq, self._activity, add="+")
        self.protocol("WM_DELETE_WINDOW", self.shutdown)
        self._register_hotkey()

        self.set_listening(not E.type_input_mode, announce=False)
        if E.setting_get("pin_hash"):
            self.lock()
        else:
            self.show("dashboard" if E.setting_get("setup_seen") else "setup")
        threading.Thread(target=self._boot, daemon=True, name="chaos-boot").start()
        self.after(100, self._poll)
        self.after(500, self._tick)
        self.after(5000, self._slow_tick)

    # ---------- chrome
    def chip(self, master, text):
        return ctk.CTkLabel(master, text=text, fg_color=PANEL, corner_radius=11, text_color=INK, font=F(12),
                            height=24, padx=10)

    def _build_bar(self):
        bar = ctk.CTkFrame(self, fg_color=WIN, corner_radius=0, height=52, border_width=0)
        bar.pack(fill="x")
        ctk.CTkFrame(self, fg_color=LINE, height=1, corner_radius=0).pack(fill="x")
        label(bar, "CHAOS Control Panel", 15, "bold").pack(side="left", padx=18, pady=12)
        self.trace_lbl = label(bar, "", 11, color=MUTE)
        self.trace_lbl.pack(side="left", padx=4)
        self.mic_btn = ctk.CTkButton(bar, text="🎤", width=36, height=36, corner_radius=18, font=F(15),
                                     command=lambda: self.set_listening(not self.engine.listening),
                                     fg_color=PANEL, hover_color=ACCENT_SOFT, border_width=1, border_color=LINE,
                                     text_color=INK)
        self.mic_btn.pack(side="right", padx=(6, 16))
        self.model_chip = self.chip(bar, "AI: starting")
        self.model_chip.pack(side="right", padx=4)
        self.mic_chip = self.chip(bar, "Mic off")
        self.mic_chip.pack(side="right", padx=4)
        self.offline_chip = self.chip(bar, "● Offline")
        self.offline_chip.configure(text_color=GOOD)
        self.offline_chip.pack(side="right", padx=4)

    def _build_rail(self):
        rail = ctk.CTkFrame(self.body, fg_color="transparent", width=200)
        rail.pack(side="left", fill="y", padx=(10, 0), pady=12)
        ctk.CTkFrame(self.body, fg_color=LINE, width=1, corner_radius=0).pack(side="left", fill="y", padx=(10, 0))
        self.nav_btns = {}
        for key, text, kbd in self.NAV:
            if key is None:
                if text:
                    label(rail, text, 11, color=MUTE).pack(fill="x", padx=12, pady=(14, 2))
                else:
                    ctk.CTkFrame(rail, fg_color=LINE, height=1).pack(fill="x", padx=8, pady=10)
                continue
            b = ctk.CTkButton(rail, text=f"{text}", anchor="w", width=180, height=34, corner_radius=8,
                              fg_color="transparent", hover_color=PANEL, text_color=INK, font=F(13),
                              border_width=1, border_color=WIN, command=lambda k=key: self.show(k))
            b.pack(pady=1)
            self.nav_btns[key] = b
        ctk.CTkFrame(rail, fg_color=LINE, height=1).pack(fill="x", padx=8, pady=10)
        ctk.CTkButton(rail, text="Lock now", anchor="w", width=180, height=34, corner_radius=8, fg_color="transparent",
                      hover_color=PANEL, text_color=INK, font=F(13), command=self.lock_now).pack()

    def update_bar(self):
        lst = self.engine.listening
        if not self.engine.mic_ok:
            self.mic_chip.configure(text="● No mic", text_color=BAD)
        else:
            self.mic_chip.configure(text="● Listening" if lst else "● Mic off", text_color=GOOD if lst else MUTE)
        self.mic_btn.configure(fg_color=ACCENT_SOFT if lst else PANEL, border_color=ACCENT if lst else LINE)
        name = E.ollama_model.split(":")[0] if E.ollama_enabled else "AI brain off"
        self.model_chip.configure(text=f"● {name}", text_color=GOOD if self.ollama_ok else BAD)

    ollama_ok = False

    # ---------- navigation
    def show(self, key):
        if self.locked:
            return
        for s in self.screens.values():
            s.pack_forget()
        scr = self.screens[key]
        scr.pack(fill="both", expand=True)
        self.current = key
        for k, b in self.nav_btns.items():
            b.configure(fg_color=ACCENT_SOFT if k == key else "transparent",
                        border_color=ACCENT if k == key else WIN, font=F(13, "bold" if k == key else "normal"))
        self.trace_lbl.configure(text=f"·  {scr.trace}")
        scr.on_show()
        scr.refresh()

    def lock(self):
        self.locked = True
        self.engine.locked = True
        self.body.pack_forget()
        self.unlock_screen.pack(fill="both", expand=True)
        self.unlock_screen.on_show()
        self.trace_lbl.configure(text="·  S1 · US-18, UC-11")

    def lock_now(self):
        if E.setting_get("pin_hash"):
            self.lock()
        else:
            self.toast("Set a PIN in Settings first")
            self.show("settings")

    def unlock(self):
        self.locked = False
        self.engine.locked = False
        self.last_activity = time.time()
        self.unlock_screen.pack_forget()
        self.body.pack(fill="both", expand=True)
        self.show(self.current or ("dashboard" if E.setting_get("setup_seen") else "setup"))

    def _activity(self, _event=None):
        self.last_activity = time.time()

    # ---------- shared helpers used by screens
    def post(self, kind, data):
        self.uiq.put((kind, data))

    def toast(self, text):
        self.toast_lbl.configure(text=f"  {text}  ")
        self.toast_lbl.place(relx=0.5, rely=0.96, anchor="s")
        if self.toast_job:
            self.after_cancel(self.toast_job)
        self.toast_job = self.after(3200, self.toast_lbl.place_forget)

    def todo_checkbox(self, master, index, todo):
        def toggle(i=index):
            todos = E.load_todos()
            if i < len(todos):
                todos[i]["done"] = not todos[i].get("done")
                E.save_todos(todos)
            self.refresh_all()
        cb = ctk.CTkCheckBox(master, text=f"{index + 1}  {todo['task']}", command=toggle, font=F(13),
                             text_color=MUTE if todo.get("done") else INK, fg_color=ACCENT, border_color=LINE,
                             checkbox_width=18, checkbox_height=18)
        cb.select() if todo.get("done") else cb.deselect()
        return cb

    def upcoming_reminders(self):
        now = datetime.datetime.now().isoformat()
        return sorted([r for r in E.load_reminders() if r.get("time", "") > now], key=lambda r: r["time"])

    def pomo_state(self):
        if not E.pomodoro_active:
            return 0, 0, "Not running"
        work, brk = E.pomodoro_work_minutes * 60, E.pomodoro_break_minutes * 60
        el = time.time() - E.pomodoro_started_at
        if el < work:
            return work - el, (work - el) / work, "Focus"
        return max(0, work + brk - el), max(0, work + brk - el) / brk, "Break"

    def pomo_text(self):
        left, _, phase = self.pomo_state()
        return f"{fmt_mmss(left)} left · {phase.lower()}" if E.pomodoro_active else "Not running"

    def refresh_all(self):
        if self.current and not self.locked:
            self.screens[self.current].refresh()
        self.screens["dashboard"].refresh()

    def set_listening(self, on, announce=True):
        if on and not self.engine.mic_ok:
            self.engine.mic_ok = True  # try again; the voice loop reports if it still fails
        self.engine.listening = on
        E.type_input_mode = not on
        E.save_preferences()
        self.update_bar()
        if announce:
            self.toast("Voice mode: listening" if on else "Type mode: microphone off")
        self.refresh_all()

    def withdraw_for_capture(self):
        self.withdraw()

    def user_said(self, text):
        self.screens["conversation"].bubble(text, True)
        self.screens["dashboard"].last_you.configure(text=f"You: {text}")

    def handle_ui_command(self, text):
        """Commands about the window itself, mirroring CHAOS's console main loop."""
        q = text.lower()
        if q in ("menu", "settings", "open menu", "show menu", "settings menu"):
            self.show("settings")
            return True
        if any(x in q for x in ["type mode", "typing mode", "keyboard mode", "text input"]):
            self.set_listening(False)
            return True
        if any(x in q for x in ["voice mode", "speaking mode", "speech mode", "talk mode"]):
            self.set_listening(True)
            return True
        return False

    # ---------- banners (S9)
    def banner(self, kind, text, tone="bad", actions=()):
        self.clear_banner(kind)
        fg, border = {"bad": (BAD_SOFT, BAD), "warn": (WARN_SOFT, WARN), "info": (INFO_SOFT, ACCENT)}[tone]
        f = ctk.CTkFrame(self.banner_area, fg_color=fg, border_width=1, border_color=border, corner_radius=8)
        f.pack(fill="x", pady=(0, 6))
        label(f, text, 13, wraplength=760).pack(side="left", padx=14, pady=10, fill="x", expand=True)
        link(f, "Dismiss", lambda: self.clear_banner(kind)).pack(side="right", padx=8)
        for name, cmd in reversed(actions):
            button(f, name, cmd, primary=True, width=10).pack(side="right", padx=4, pady=8)
        self.banners[kind] = f

    def clear_banner(self, kind):
        f = self.banners.pop(kind, None)
        if f:
            f.destroy()

    # ---------- background start-up
    def _boot(self):
        print("[CHAOS] Starting engine...")
        models = ollama_models()
        if models is None and find_ollama_exe():
            print("[CHAOS] Ollama installed but not running; starting it")
            start_ollama()
            models = ollama_models()
        if models is None:
            self.post("ollama_down", None)
        else:
            self.post("ollama_ok", models)
            if not has_model(models, E.ollama_model.split(":")[0]):
                E.resolve_ollama_model()
            if models:
                E.warmup_ollama()
            else:
                self.post("no_model", None)
        try:
            E.get_whisper_model()
        except Exception as e:
            print(f"[Warning] {e}")
        try:
            import speech_recognition as sr
            if sr.Microphone.list_microphone_names():
                E.calibrate_microphone()
            else:
                self.post("nomic", "No microphone found")
        except Exception as e:
            self.post("nomic", str(e))
        self._rearm_reminders()
        E.report_memory_usage()
        self.post("booted", None)

    def _rearm_reminders(self):
        now = datetime.datetime.now()
        for r in E.load_reminders():
            try:
                due = datetime.datetime.fromisoformat(r["time"])
            except (KeyError, ValueError):
                continue
            if due > now:
                delay = (due - now).total_seconds()
                threading.Timer(delay, lambda m=r["message"]: E.speak(f"Reminder: {m}", force_voice=True)).start()

    def _register_hotkey(self):
        try:
            import keyboard
            keyboard.add_hotkey("win+shift+c", lambda: self.post("hotkey", None))
            print("[Global hotkey registered: Win+Shift+C]")
        except Exception as e:
            print(f"[Hotkey not available: {e}]")

    # ---------- event pump
    def _poll(self):
        try:
            while True:
                kind, data = self.uiq.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, kind, data):
        conv, dash = self.screens["conversation"], self.screens["dashboard"]
        if kind == "reply":
            text, elapsed = data
            conv.bubble(text, False, meta=f"{elapsed:.1f} s" if elapsed is not None else None)
            dash.last_chaos.configure(text=f"CHAOS: {text}")
            if text == E.OFFLINE_BLOCK_MESSAGE:
                self.banner("offline", "That needs the internet. Offline mode is on, so CHAOS did not connect and nothing was sent.",
                            tone="info")
                self.after(9000, lambda: self.clear_banner("offline"))
            if "Ollama is not running" in text or "Ollama chal nahi raha" in text:
                self._handle("ollama_down", None)
            if text.startswith("Heads up!"):
                self.screens["truesight"].alert.configure(text=f"{time.strftime('%H:%M')}  {text}", text_color=INK)
        elif kind == "heard":
            self.user_said(data[0])
            self.last_activity = time.time()
        elif kind == "busy":
            if not data:
                self.refresh_all()
        elif kind == "voice_state":
            pass
        elif kind == "nomic":
            self.engine.mic_ok = False
            self.update_bar()
            self.banner("mic", "No microphone found. CHAOS can't hear you right now. Plug in a microphone, or type instead.",
                        tone="warn", actions=[("Switch to type mode", lambda: (self.set_listening(False), self.clear_banner("mic"),
                                                                                self.show("conversation")))])
        elif kind == "ollama_down":
            self.ollama_ok = False
            self.update_bar()
            exe = find_ollama_exe()
            self.banner("ollama", "AI brain unavailable: Ollama is not running on this PC. Commands still work; questions and True Sight need it.",
                        actions=[("Start Ollama", self._retry_ollama)] if exe else
                        [("Get Ollama", lambda: webbrowser.open("https://ollama.com/download")), ("Retry", self._retry_ollama)])
        elif kind == "ollama_ok":
            self.ollama_ok = True
            self.clear_banner("ollama")
            self.update_bar()
        elif kind == "no_model":
            self.banner("model", "No AI model is installed yet. Download Llama 3.2 (2.0 GB) once to enable answers and True Sight.",
                        tone="warn", actions=[("Open system check", lambda: self.show("setup"))])
        elif kind == "hotkey":
            self.deiconify()
            self.lift()
            if not self.locked:
                self.set_listening(not self.engine.listening)
        elif kind == "exit":
            self.after(1500, self.shutdown)
        elif kind == "restore_window":
            self.deiconify()
            self.lift()
        elif kind == "scan_stage":
            self.screens["truesight"].answer.configure(text=data)
        elif kind == "scan_done":
            self.screens["truesight"].done(*data)
        elif kind == "setup_rows":
            self.screens["setup"].show_rows(data)
        elif kind == "pull_progress":
            frac, text = data
            s = self.screens["setup"]
            if frac is not None:
                s.pull_bar.set(frac)
            s.set_row("model", True if frac == 1.0 else None, text)
        elif kind == "pull_done":
            if data:
                self.clear_banner("model")
                self.toast("Llama 3.2 installed")
                self.update_bar()
        elif kind == "booted":
            self.update_bar()
            self.refresh_all()

    def _retry_ollama(self):
        self.clear_banner("ollama")
        self.toast("Checking Ollama...")

        def work():
            models = ollama_models()
            if models is None:
                start_ollama()
                models = ollama_models()
            if models is None:
                self.post("ollama_down", None)
            else:
                self.post("ollama_ok", models)
                if not models:
                    self.post("no_model", None)
                else:
                    E.resolve_ollama_model()
        threading.Thread(target=work, daemon=True).start()

    def _tick(self):
        self.screens["dashboard"].tick()
        if self.current == "productivity":
            self.screens["productivity"].tick()
        mins = E.setting_get("auto_lock_minutes", 15)
        if (not self.locked and mins and E.setting_get("pin_hash")
                and time.time() - self.last_activity > mins * 60):
            self.lock()
        self.after(150, self._tick)

    def _slow_tick(self):
        if self.current == "performance" and not self.locked:
            self.screens["performance"].refresh()
        if self.current == "dashboard" and not self.locked:
            self.screens["dashboard"].refresh()
        self.after(5000, self._slow_tick)

    def shutdown(self):
        self.engine.running = False
        try:
            E.stop_pomodoro()
            E.stop_screen_watcher()
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.destroy()
        finally:
            os._exit(0)


def main():
    app = ChaosApp()
    app.mainloop()


if __name__ == "__main__":
    main()
