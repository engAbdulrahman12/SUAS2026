"""SUAS 2026 — Mission Planning GUI  (v4)

Layout: two-column split
  LEFT  (380px) — connection, MAVProxy, log panel (scrollable, large font)
  RIGHT (fill)  — tabs: Mission Waypoints | Search Area  (full height)

Footer bar pinned at bottom: Abort | status | Continue → | Start Mission
"""

import json, os, subprocess, sys, threading
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from dataclasses import dataclass
import config

ctk.set_appearance_mode("dark")

# ── Tokens ───────────────────────────────────────────────────────
FONT       = "Segoe UI"
BG         = "#12141a"
CARD       = "#1a1d26"
CARD2      = "#1f2230"
BORDER     = "#2c3040"
TEXT       = "#e2e8f0"
MUTED      = "#7c87a0"
PRIMARY    = "#4f46e5"
PRIMARY_H  = "#4338ca"
SUCCESS    = "#16a34a"
SUCCESS_H  = "#15803d"
NEUTRAL    = "#272b38"
NEUTRAL_H  = "#323748"
WARN       = "#d97706"
WARN_H     = "#b45309"
DANGER     = "#dc2626"
DANGER_H   = "#b91c1c"
ACCENT_S   = "#10b981"
ACCENT_E   = "#ef4444"
LOG_BG     = "#0a0c11"
LOG_OK     = "#4ade80"
LOG_ERR    = "#f87171"
LOG_INFO   = "#93c5fd"
LOG_WARN   = "#fbbf24"
LOG_MAV    = "#c084fc"
LOG_PLAIN  = "#cbd5e1"

# ── Helpers ──────────────────────────────────────────────────────

def _ports():
    try:
        import serial.tools.list_ports
        p = [x.device for x in serial.tools.list_ports.comports()]
        return sorted(p) if p else ["(no ports found)"]
    except ImportError:
        return ["(install pyserial)"]

def _e(parent, w=180, h=34, fs=12, **kw):
    return ctk.CTkEntry(parent, width=w, height=h, corner_radius=6,
                        fg_color=CARD2, border_color=BORDER, border_width=1,
                        text_color=TEXT, font=(FONT, fs), **kw)

def _btn(parent, text, w=None, h=34, fg=NEUTRAL, hv=NEUTRAL_H,
         tc=TEXT, fs=11, bold=False, cmd=None, **kw):
    font = (FONT, fs, "bold") if bold else (FONT, fs)
    kw2 = {"width": w} if w else {}
    return ctk.CTkButton(parent, text=text, height=h, corner_radius=7,
                         fg_color=fg, hover_color=hv, text_color=tc,
                         font=font, command=cmd, **kw2, **kw)

def _label(parent, text, fs=12, tc=TEXT, bold=False, **kw):
    font = (FONT, fs, "bold") if bold else (FONT, fs)
    return ctk.CTkLabel(parent, text=text, text_color=tc, font=font, **kw)

@dataclass
class MissionParams:
    waypoints: list
    laps: int
    uri: str
    search_corners: object   # [(lat,lon,alt) x4] (A,B,C,D) or None to skip
    confirmed: bool = False

# ── Log redirect ─────────────────────────────────────────────────

class LogRedirect:
    def __init__(self, cb):
        self._cb = cb
        self._o  = sys.stdout
        self._e  = sys.stderr
    def write(self, t):
        if t.strip(): self._cb(t.rstrip())
        self._o.write(t)
    def flush(self): self._o.flush()
    def install(self):   sys.stdout = sys.stderr = self
    def uninstall(self): sys.stdout = self._o; sys.stderr = self._e

# ── GUI ──────────────────────────────────────────────────────────

class MissionGUI:

    def __init__(self):
        self.rows           = []
        self.row_frames     = []
        self._mav_proc      = None
        self._mission_thd   = None
        self._conn          = None
        self._continue_ev   = threading.Event()

        # search / camera phase
        self._camera               = None
        self._cam_active           = False
        self._click_to_fly_enabled = False
        self._cam_photo            = None
        self._cam_native_w         = None
        self._cam_native_h         = None

        self.root = ctk.CTk()
        self._is_sim = tk.BooleanVar(value=bool(config.TEST_FLAG))

        self.root.title("SUAS 2026 — Mission Planner")
        self.root.geometry("1280x900")
        self.root.minsize(1100, 750)
        self.root.configure(fg_color=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self._lr = LogRedirect(self._log)
        self._lr.install()

        self._build()
        self._log("GUI ready — select mode, load JSON, click Start Mission.", "info")
        self.root.mainloop()

    def _close(self):
        if self._mission_thd and self._mission_thd.is_alive():
            if not messagebox.askyesno("Quit", "Mission running — abort and quit?"): return
        self._lr.uninstall()
        self._kill_mav()
        self._stop_camera()
        self.root.destroy()

    # ════════════════════════════════════════════════════════════
    #  BUILD
    # ════════════════════════════════════════════════════════════

    def _build(self):
        self._build_header()

        # ── Footer pinned at bottom ──────────────────────────────
        self._build_footer()

        # ── Main body (left | right) ─────────────────────────────
        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(10, 8))
        body.columnconfigure(0, minsize=370, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")

        self._build_left(left)
        self._build_right(right)

    # ── Header ───────────────────────────────────────────────────

    def _build_header(self):
        h = ctk.CTkFrame(self.root, fg_color="#0d0f14", corner_radius=0, height=52)
        h.pack(fill="x")
        h.pack_propagate(False)
        inner = ctk.CTkFrame(h, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18)

        _label(inner, "SUAS 2026", fs=15, bold=True, tc="white").pack(side="left", pady=12)
        _label(inner, "  Mission Planner", fs=12, tc=MUTED).pack(side="left", pady=12)

        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="right", fill="y")

        home = (f"{config.HOME_LAT:.5f}, {config.HOME_LON:.5f}"
                if config.HOME_LAT else "auto GPS")
        _label(right, f"Alt {config.MISSION_ALT} m AGL  ·  Home: {home}",
               fs=10, tc=MUTED).pack(side="left", padx=(0, 14), pady=12)

        self._badge = ctk.CTkLabel(right, text="", corner_radius=6,
                                   font=(FONT, 10, "bold"), padx=10, pady=3)
        self._badge.pack(side="left", pady=12)
        self._refresh_badge()

    def _refresh_badge(self):
        if self._is_sim.get():
            self._badge.configure(text="● SIMULATION", text_color="#7dd3fc", fg_color="#1e3a5f")
        else:
            self._badge.configure(text="● REAL DRONE", text_color="#fdba74", fg_color="#5f2e1e")

    # ── Footer ───────────────────────────────────────────────────

    def _build_footer(self):
        foot = ctk.CTkFrame(self.root, fg_color=CARD, corner_radius=0, height=58)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        inner = ctk.CTkFrame(foot, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=9)

        self._abort_btn = _btn(inner, "⚠  RTL / Abort", h=40, w=130,
                               fg=DANGER, hv=DANGER_H, tc="white", fs=11, bold=True,
                               cmd=self._abort, state="disabled")
        self._abort_btn.pack(side="left")

        self._status_lbl = _label(inner, "Ready", fs=11, tc=MUTED)
        self._status_lbl.pack(side="left", padx=16)

        self._start_btn = _btn(inner, "▶  Start Mission", h=40,
                               fg=SUCCESS, hv=SUCCESS_H, tc="white", fs=12, bold=True,
                               cmd=self._confirm)
        self._start_btn.pack(side="right")

        _btn(inner, "Cancel", h=40, w=80, fg="transparent", hv=NEUTRAL,
             tc=MUTED, fs=11, cmd=self._close,
             border_width=1, border_color=BORDER).pack(side="right", padx=(0, 8))

        self._cont_btn = _btn(inner, "Continue →", h=40, w=110,
                              fg=PRIMARY, hv=PRIMARY_H, tc="white", fs=11, bold=True,
                              cmd=self._user_continue)
        # hidden until needed

    def _set_status(self, txt, color=MUTED):
        try: self._status_lbl.configure(text=txt, text_color=color)
        except Exception: pass

    # ════════════════════════════════════════════════════════════
    #  LEFT PANEL
    # ════════════════════════════════════════════════════════════

    def _build_left(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        self._build_conn_card(parent)   # row 0
        self._build_log_card(parent)    # row 1 (expands)

    # ── Connection card ──────────────────────────────────────────

    def _build_conn_card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        p = ctk.CTkFrame(card, fg_color="transparent")
        p.pack(fill="x", padx=16, pady=14)

        _label(p, "CONNECTION", fs=9, tc=MUTED, bold=True).pack(anchor="w", pady=(0, 10))

        # Mode row
        mr = ctk.CTkFrame(p, fg_color="transparent")
        mr.pack(fill="x", pady=(0, 6))
        _label(mr, "Mode", fs=11, tc=TEXT).pack(side="left", padx=(0, 8))

        self._sim_btn = _btn(mr, "SITL", w=70, h=32, fg=PRIMARY, hv=PRIMARY_H,
                             tc="white", fs=10, bold=True, cmd=self._set_sim)
        self._sim_btn.pack(side="left", padx=(0, 3))
        self._real_btn = _btn(mr, "Real Drone", w=95, h=32, fg=NEUTRAL, hv=NEUTRAL_H,
                              tc=TEXT, fs=10, bold=True, cmd=self._set_real)
        self._real_btn.pack(side="left")

        # URI row
        ur = ctk.CTkFrame(p, fg_color="transparent")
        ur.pack(fill="x", pady=(0, 6))
        _label(ur, "URI", fs=11, tc=TEXT).pack(side="left", padx=(0, 8))
        self.uri_var = tk.StringVar(value=config.default_uri())
        _e(ur, w=220, h=32).configure(textvariable=self.uri_var)
        e = _e(ur, w=220, h=32, textvariable=self.uri_var)
        e.pack(side="left", padx=(0, 6))

        # Quick URI presets
        qr = ctk.CTkFrame(p, fg_color=CARD2, corner_radius=6)
        qr.pack(fill="x", pady=(0, 10))
        qf = ctk.CTkFrame(qr, fg_color="transparent")
        qf.pack(fill="x", padx=6, pady=5)
        for lbl, uri in [("SITL TCP","tcp:127.0.0.1:5762"),
                          ("MP UDP","udp:0.0.0.0:14550"),
                          ("Script UDP","udp:0.0.0.0:14552")]:
            _btn(qf, lbl, h=26, fs=9, bold=True,
                 cmd=lambda u=uri: self.uri_var.set(u)).pack(side="left", padx=2)

        # Separator
        ctk.CTkFrame(p, fg_color=BORDER, height=1).pack(fill="x", pady=(0, 10))

        # COM port row
        cr = ctk.CTkFrame(p, fg_color="transparent")
        cr.pack(fill="x", pady=(0, 6))
        _label(cr, "COM Port", fs=11, tc=TEXT).pack(side="left", padx=(0, 8))
        self._port_var  = tk.StringVar(value=config.COM_PORT)
        self._port_menu = ctk.CTkOptionMenu(
            cr, variable=self._port_var, values=_ports(),
            width=118, height=32, corner_radius=6,
            fg_color=CARD2, button_color=NEUTRAL, button_hover_color=NEUTRAL_H,
            text_color=TEXT, font=(FONT, 11))
        self._port_menu.pack(side="left", padx=(0, 5))
        self._ref_btn = _btn(cr, "↻", w=34, h=32, fs=13, cmd=self._refresh_ports)
        self._ref_btn.pack(side="left")

        # MAVProxy row
        mr2 = ctk.CTkFrame(p, fg_color="transparent")
        mr2.pack(fill="x", pady=(0, 4))
        self._mav_btn = _btn(mr2, "▶  Start MAVProxy", h=34,
                             fg=WARN, hv=WARN_H, tc="white", fs=10, bold=True,
                             cmd=self._toggle_mav)
        self._mav_btn.pack(side="left", padx=(0, 8))
        self._mav_status = _label(mr2, "● Stopped", fs=10, tc=MUTED)
        self._mav_status.pack(side="left")

        # Hint
        _label(p, "SITL: no MAVProxy needed.  Real: COM → MAVProxy → Mission.",
               fs=9, tc=MUTED).pack(anchor="w", pady=(6, 0))

        # Apply initial state
        if config.TEST_FLAG:
            self.root.after(80, lambda: self._mav_controls("disabled"))

    def _mav_controls(self, state):
        dim = MUTED if state == "disabled" else TEXT
        try:
            self._port_menu.configure(state=state, text_color=dim)
            self._mav_btn.configure(state=state)
            self._ref_btn.configure(state=state)
        except Exception: pass

    def _set_sim(self):
        self._is_sim.set(True); config.TEST_FLAG = 1
        self.uri_var.set("tcp:127.0.0.1:5762")
        self._sim_btn.configure(fg_color=PRIMARY, hover_color=PRIMARY_H)
        self._real_btn.configure(fg_color=NEUTRAL, hover_color=NEUTRAL_H)
        self._refresh_badge()
        self._mav_controls("disabled")
        self._log("[MODE] SITL — no MAVProxy needed.", "info")

    def _set_real(self):
        self._is_sim.set(False); config.TEST_FLAG = 0
        self.uri_var.set("udp:0.0.0.0:14552")
        self._sim_btn.configure(fg_color=NEUTRAL, hover_color=NEUTRAL_H)
        self._real_btn.configure(fg_color=WARN, hover_color=WARN_H)
        self._refresh_badge()
        self._mav_controls("normal")
        self._log("[MODE] Real Drone — select COM, start MAVProxy.", "warn")

    def _refresh_ports(self):
        pts = _ports()
        self._port_menu.configure(values=pts)
        if pts: self._port_var.set(pts[0])
        self._log(f"[PORTS] {', '.join(pts)}", "info")

    # ── MAVProxy ─────────────────────────────────────────────────

    def _toggle_mav(self):
        if self._mav_proc and self._mav_proc.poll() is None: self._kill_mav()
        else: self._start_mav()

    def _start_mav(self):
        port = self._port_var.get()
        if any(x in port for x in ["no ports","install"]):
            messagebox.showerror("MAVProxy", "No valid COM port."); return
        mp = self._find_mav()
        if not mp:
            messagebox.showerror("MAVProxy not found","Run: py -m pip install mavproxy"); return
        cmd = [sys.executable, mp, f"--master={port}",
               f"--baudrate={config.BAUD_RATE}",
               "--out=udp:127.0.0.1:14550", "--out=udp:127.0.0.1:14552"]
        self._log(f"[MAVProxy] {' '.join(cmd)}", "info")
        try:
            self._mav_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as e:
            self._log(f"[MAVProxy] Failed: {e}", "error"); return
        self._mav_btn.configure(text="■  Stop MAVProxy", fg_color=DANGER, hover_color=DANGER_H)
        self._mav_status.configure(text=f"● Running on {port}", text_color=LOG_OK)
        threading.Thread(target=self._mav_stream, daemon=True).start()

    def _find_mav(self):
        import shutil, site
        f = shutil.which("mavproxy.py")
        if f: return f
        s = os.path.join(os.path.dirname(sys.executable), "Scripts", "mavproxy.py")
        if os.path.exists(s): return s
        try:
            for d in site.getsitepackages():
                p = os.path.join(os.path.dirname(d), "Scripts", "mavproxy.py")
                if os.path.exists(p): return p
        except Exception: pass
        return None

    def _mav_stream(self):
        for line in self._mav_proc.stdout:
            line = line.rstrip()
            if line:
                tag = "error" if "ERROR" in line.upper() else \
                      "warn"  if "WARN"  in line.upper() else "mav"
                self._log(f"[MAV] {line}", tag)
        self._log("[MAVProxy] Stopped.", "warn")
        self.root.after(0, self._mav_stopped_ui)

    def _kill_mav(self):
        if self._mav_proc:
            try: self._mav_proc.terminate()
            except Exception: pass
            self._mav_proc = None
        self._mav_stopped_ui()

    def _mav_stopped_ui(self):
        try:
            self._mav_btn.configure(text="▶  Start MAVProxy", fg_color=WARN, hover_color=WARN_H)
            self._mav_status.configure(text="● Stopped", text_color=MUTED)
        except Exception: pass

    # ── Log card ─────────────────────────────────────────────────

    def _build_log_card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        card.grid(row=1, column=0, sticky="nsew")
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        # Header
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        _label(hdr, "MISSION LOG", fs=10, tc=MUTED, bold=True).pack(side="left")
        _btn(hdr, "Clear", w=52, h=24, fs=9, cmd=self._clear_log).pack(side="right")

        # Text box — large font, fills remaining height
        self._log_box = tk.Text(
            card, bg=LOG_BG, fg=LOG_PLAIN,
            font=("Consolas", 12),          # bigger font
            relief="flat", bd=0, wrap="word",
            state="disabled", padx=10, pady=8,
            insertbackground=TEXT, selectbackground=PRIMARY)
        self._log_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))

        sb = ctk.CTkScrollbar(card, command=self._log_box.yview,
                              button_color=NEUTRAL, button_hover_color=NEUTRAL_H)
        sb.grid(row=1, column=1, sticky="ns", pady=(0, 8), padx=(0, 4))
        self._log_box.configure(yscrollcommand=sb.set)

        self._log_box.tag_config("ok",    foreground=LOG_OK)
        self._log_box.tag_config("error", foreground=LOG_ERR)
        self._log_box.tag_config("info",  foreground=LOG_INFO)
        self._log_box.tag_config("warn",  foreground=LOG_WARN)
        self._log_box.tag_config("mav",   foreground=LOG_MAV)
        self._log_box.tag_config("plain", foreground=LOG_PLAIN)

    def _log(self, text: str, tag: str = "plain"):
        def _do():
            self._log_box.configure(state="normal")
            if tag == "plain":
                if any(x in text for x in ["✓","OK","ready","accepted","Armed","complete","onnected"]):
                    t = "ok"
                elif any(x in text for x in ["Error","error","FAIL","fail","Timeout","timeout"]):
                    t = "error"
                elif any(x in text for x in ["warn","WARN","RTL","Interrupt","Abort"]):
                    t = "warn"
                else:
                    t = "plain"
            else:
                t = tag
            self._log_box.insert("end", text + "\n", t)
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        try: self.root.after(0, _do)
        except Exception: pass

    def _clear_log(self):
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    # ════════════════════════════════════════════════════════════
    #  RIGHT PANEL — tabs
    # ════════════════════════════════════════════════════════════

    def _build_right(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(
            parent, fg_color=CARD,
            segmented_button_fg_color=CARD2,
            segmented_button_selected_color=PRIMARY,
            segmented_button_selected_hover_color=PRIMARY_H,
            segmented_button_unselected_color=CARD2,
            text_color=TEXT, corner_radius=12)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tabs.add("Mission Waypoints")
        self.tabs.add("Search Area")
        self.tabs.add("Camera Feed")
        self._build_wp_tab(self.tabs.tab("Mission Waypoints"))
        self._build_search_tab(self.tabs.tab("Search Area"))
        self._build_camera_tab(self.tabs.tab("Camera Feed"))

    # ── Waypoints tab ────────────────────────────────────────────

    def _build_wp_tab(self, tab):
        tab.configure(fg_color="transparent")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        # Toolbar
        tb = ctk.CTkFrame(tab, fg_color=CARD2, corner_radius=8)
        tb.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 8))
        tf = ctk.CTkFrame(tb, fg_color="transparent")
        tf.pack(fill="x", padx=12, pady=10)

        _btn(tf, "📂  Load JSON", h=36, fg=PRIMARY, hv=PRIMARY_H,
             tc="white", fs=12, bold=True, cmd=self._load_json).pack(side="left")

        ctk.CTkFrame(tf, fg_color=BORDER, width=1, height=26).pack(side="left", padx=14)

        _label(tf, "Laps", fs=12).pack(side="left")
        self.laps_var = tk.IntVar(value=config.DEFAULT_LAPS)
        ctk.CTkLabel(tf, textvariable=self.laps_var, width=30, height=30,
                     fg_color=CARD, corner_radius=6, text_color=TEXT,
                     font=(FONT, 12, "bold")).pack(side="left", padx=8)
        _btn(tf, "−", w=30, h=30, fs=14,
             cmd=lambda: self.laps_var.set(max(1, self.laps_var.get()-1))
             ).pack(side="left", padx=(0, 2))
        _btn(tf, "+", w=30, h=30, fs=14,
             cmd=lambda: self.laps_var.set(min(20, self.laps_var.get()+1))
             ).pack(side="left", padx=(0, 14))

        ctk.CTkFrame(tf, fg_color=BORDER, width=1, height=26).pack(side="left", padx=0)

        _btn(tf, "+ Add Row", h=36, fs=11,
             cmd=self._add_row).pack(side="left", padx=(14, 3))
        _btn(tf, "Remove Last", h=36, fs=11,
             cmd=self._remove_last).pack(side="left", padx=3)
        _btn(tf, "Clear All", h=36, fs=11,
             cmd=self._clear_rows).pack(side="left", padx=3)

        # Table
        tbl = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        tbl.grid(row=1, column=0, sticky="nsew", padx=6)
        tbl.rowconfigure(1, weight=1)
        tbl.columnconfigure(0, weight=1)

        # Column headers
        hdr = ctk.CTkFrame(tbl, fg_color=CARD2, corner_radius=6)
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        hf = ctk.CTkFrame(hdr, fg_color="transparent")
        hf.pack(fill="x", padx=10, pady=6)
        for txt, w in [("#", 32), ("LATITUDE", 0), ("LONGITUDE", 0),
                        ("ALT AGL (m)", 120), ("NAME", 130)]:
            kw = {"width": w} if w else {}
            lbl = ctk.CTkLabel(hf, text=txt, text_color=MUTED,
                               font=(FONT, 10, "bold"), anchor="w", **kw)
            if w:
                lbl.pack(side="left", padx=4)
            else:
                lbl.pack(side="left", padx=4, expand=True, fill="x")

        # Scrollable rows
        self.scroll = ctk.CTkScrollableFrame(
            tbl, fg_color="transparent",
            scrollbar_button_color=NEUTRAL,
            scrollbar_button_hover_color=NEUTRAL_H)
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))

        for _ in range(4):
            self._add_row()

    def _add_row(self, lat="", lon="", alt="", name=""):
        idx = len(self.rows) + 1
        bg  = CARD if idx % 2 else CARD2
        row = ctk.CTkFrame(self.scroll, fg_color=bg, corner_radius=7)
        row.pack(fill="x", pady=3)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)

        ctk.CTkLabel(row, text=str(idx), width=32, text_color=MUTED,
                     font=(FONT, 11, "bold")).grid(row=0, column=0, padx=(8,4), pady=8)

        lat_e  = _e(row, w=10, h=36, fs=12); lat_e.insert(0, lat)
        lon_e  = _e(row, w=10, h=36, fs=12); lon_e.insert(0, lon)
        alt_e  = _e(row, w=110, h=36, fs=12); alt_e.insert(0, alt)
        name_e = _e(row, w=120, h=36, fs=12); name_e.insert(0, name)

        lat_e.grid(row=0, column=1, padx=4, pady=8, sticky="ew")
        lon_e.grid(row=0, column=2, padx=4, pady=8, sticky="ew")
        alt_e.grid(row=0, column=3, padx=4, pady=8)
        name_e.grid(row=0, column=4, padx=(4, 8), pady=8)

        self.rows.append((lat_e, lon_e, alt_e, name_e))
        self.row_frames.append(row)

    def _remove_last(self):
        if self.rows:
            self.rows.pop()
            self.row_frames.pop().destroy()

    def _clear_rows(self):
        for r in self.row_frames: r.destroy()
        self.rows.clear(); self.row_frames.clear()

    def _load_json(self):
        path = filedialog.askopenfilename(
            title="Select waypoints JSON",
            filetypes=[("JSON","*.json"),("All","*.*")])
        if not path: return
        try:
            data = json.load(open(path, encoding="utf-8"))
            wps  = data.get("waypoints", [])
            if not wps:
                messagebox.showwarning("Empty","No 'waypoints' key."); return
            if "default_laps" in data:
                self.laps_var.set(int(data["default_laps"]))
            self._clear_rows()
            for wp in wps:
                self._add_row(str(wp.get("lat","")), str(wp.get("lon","")),
                              str(wp.get("alt","")), str(wp.get("name","")))
            corners = data.get("search_corners")
            if corners and len(corners) == 4:
                for tag, d in zip(["A", "B", "C", "D"], corners):
                    lat_e, lon_e, alt_e = self._search_entries[tag]
                    for w, k in zip((lat_e, lon_e, alt_e), ("lat", "lon", "alt")):
                        w.delete(0, tk.END); w.insert(0, str(d.get(k, "")))
            if "search_swath" in data:
                self._log("[JSON] Note: 'search_swath' in file is no longer used "
                          "(single straight-line pass doesn't need swath spacing).", "info")
            self._log(f"[JSON] Loaded {len(wps)} waypoints from {os.path.basename(path)}", "ok")
        except Exception as e:
            self._log(f"[JSON] Error: {e}", "error")
            messagebox.showerror("Load error", str(e))

    # ── Search tab ────────────────────────────────────────────────

    def _build_search_tab(self, tab):
        tab.configure(fg_color="transparent")

        # Info banner
        info = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        info.pack(fill="x", padx=6, pady=(4, 10))
        ip = ctk.CTkFrame(info, fg_color="transparent")
        ip.pack(fill="x", padx=16, pady=14)
        _label(ip, "4 corners define the search-area boundary (A, B = entry edge; D, C = exit edge).",
               fs=12).pack(anchor="w")
        _label(ip, "Drone flies one straight line down the centre of the area, along its "
                    "longest side — then hands off to manual click-to-fly search.",
               fs=11, tc=MUTED).pack(anchor="w", pady=(4, 0))
        _label(ip, "After the laps finish you'll be asked: Return Home, or go run this search. Leave blank to skip.",
               fs=11, tc=MUTED).pack(anchor="w", pady=(2, 0))

        # Diagram
        dg = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        dg.pack(fill="x", padx=6, pady=(0, 10))
        cv = tk.Canvas(dg, width=360, height=110, bg=CARD, highlightthickness=0)
        cv.pack(pady=12, padx=20, anchor="w")
        cv.create_rectangle(40, 15, 320, 90, outline=BORDER, width=2, fill=CARD2)
        for y in (30, 50, 70):
            cv.create_line(45, y, 315, y, fill=PRIMARY, width=2)
        cv.create_oval(30, 8, 50, 22, fill=ACCENT_S, outline="");  cv.create_text(20, 8,  text="A", font=(FONT,10,"bold"), fill=ACCENT_S)
        cv.create_oval(30, 83, 50, 97, fill=ACCENT_S, outline=""); cv.create_text(20, 90, text="D", font=(FONT,10,"bold"), fill=ACCENT_S)
        cv.create_oval(310, 8, 330, 22, fill=ACCENT_E, outline="");  cv.create_text(340, 8,  text="B", font=(FONT,10,"bold"), fill=ACCENT_E)
        cv.create_oval(310, 83, 330, 97, fill=ACCENT_E, outline=""); cv.create_text(340, 90, text="C", font=(FONT,10,"bold"), fill=ACCENT_E)
        cv.create_text(180, 104, text="single straight-line pass along the longest side", font=(FONT, 9), fill=MUTED)

        # Coordinate inputs
        ic = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        ic.pack(fill="x", padx=6)
        g = ctk.CTkFrame(ic, fg_color="transparent")
        g.pack(fill="x", padx=18, pady=18)
        g.columnconfigure(1, weight=1)
        g.columnconfigure(2, weight=1)

        for col, h in enumerate(["", "LATITUDE", "LONGITUDE", "ALT AGL (m)"]):
            ctk.CTkLabel(g, text=h, font=(FONT, 10, "bold"), text_color=MUTED,
                         anchor="w").grid(row=0, column=col, padx=6, pady=(0, 8), sticky="w")

        self._search_entries = {}
        corner_labels = [("A", "Entry left", ACCENT_S),
                          ("B", "Entry right", ACCENT_E),
                          ("C", "Exit right", ACCENT_E),
                          ("D", "Exit left", ACCENT_S)]
        for row, (tag, desc, color) in enumerate(corner_labels, 1):
            ctk.CTkLabel(g, text=f"● {tag} — {desc}", text_color=color,
                         font=(FONT, 13, "bold"), anchor="w").grid(row=row, column=0, padx=6, sticky="w")
            lat_e = _e(g, w=10, h=36, fs=12); lat_e.grid(row=row, column=1, padx=6, pady=6, sticky="ew")
            lon_e = _e(g, w=10, h=36, fs=12); lon_e.grid(row=row, column=2, padx=6, pady=6, sticky="ew")
            alt_e = _e(g, w=110, h=36, fs=12); alt_e.grid(row=row, column=3, padx=6, pady=6)
            self._search_entries[tag] = (lat_e, lon_e, alt_e)

        _label(g, "Leave altitude blank to use Mission Alt from config.",
               fs=10, tc=MUTED).grid(row=5, column=1, columnspan=3, sticky="w", padx=6, pady=(4, 12))

    def _parse_search(self):
        vals = {}
        for tag, (lat_e, lon_e, alt_e) in self._search_entries.items():
            vals[tag] = (lat_e.get().strip(), lon_e.get().strip(), alt_e.get().strip())

        flat = [v for triple in vals.values() for v in triple[:2]]
        if not any(flat):
            return None
        if not all(flat):
            messagebox.showerror("Search", "Fill all 4 corners (lat/lon) or leave all blank.")
            return "ERR"

        corners = []
        try:
            for tag in ["A", "B", "C", "D"]:
                la, lo, al = vals[tag]
                lat, lon = float(la), float(lo)
                alt = float(al) if al else config.MISSION_ALT
                corners.append((lat, lon, alt))
        except ValueError:
            messagebox.showerror("Search", "Invalid coordinates.")
            return "ERR"

        return corners

    # ── Camera tab ───────────────────────────────────────────────

    def _build_camera_tab(self, tab):
        tab.configure(fg_color="transparent")
        tab.rowconfigure(1, weight=1)
        tab.columnconfigure(0, weight=1)

        info = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        info.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 8))
        ip = ctk.CTkFrame(info, fg_color="transparent")
        ip.pack(fill="x", padx=16, pady=10)
        self._cam_info_lbl = _label(
            ip, "Feed starts automatically when the drone reaches the search area.",
            fs=11, tc=MUTED)
        self._cam_info_lbl.pack(anchor="w")
        _label(ip, "Source is set by config.CAMERA_MODE ('webcam' or 'rtsp') — independent of "
                    "SITL/Real Drone, so you can fly the real drone without a camera wired up yet. "
                    "'rtsp' mode also runs AI detection (tents / mannequins). "
                    "After mapping completes, click the feed to fly the drone to that real-world "
                    "GPS point (localized from your current position + heading). "
                    "Click once first, then press U to climb / D to descend "
                    f"({config.CLICK_ALT_STEP_M:.1f} m per press).",
               fs=10, tc=MUTED).pack(anchor="w", pady=(2, 0))

        holder = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        holder.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self.cam_canvas = tk.Canvas(holder, bg="#000000", highlightthickness=0)
        self.cam_canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.cam_canvas.create_text(
            300, 200, text="Camera feed inactive", fill=MUTED, font=(FONT, 13),
            tags="placeholder")
        self.cam_canvas.bind("<Button-1>", self._on_camera_click)
        self.cam_canvas.bind("<KeyPress-d>", self._on_alt_key)
        self.cam_canvas.bind("<KeyPress-D>", self._on_alt_key)
        self.cam_canvas.bind("<KeyPress-u>", self._on_alt_key)
        self.cam_canvas.bind("<KeyPress-U>", self._on_alt_key)

    # ── Camera feed lifecycle ────────────────────────────────────

    def _start_camera(self):
        try:
            from vision import CameraWorker
            self._camera = CameraWorker()
            self._camera.start()
        except Exception as e:
            self._log(f"[VISION] Camera failed to start: {e}", "error")
            self._camera = None
            return
        self._cam_active = True
        self.root.after(0, lambda: self._cam_info_lbl.configure(
            text=("RTSP feed + AI detection running." if config.CAMERA_MODE == "rtsp"
                  else "Webcam feed (CAMERA_MODE='webcam', no AI) — flip config.CAMERA_MODE "
                       "to 'rtsp' once the real camera is wired up.")))
        self.root.after(0, self._update_camera_frame)

    def _stop_camera(self):
        self._cam_active = False
        self._click_to_fly_enabled = False
        if self._camera:
            self._camera.stop()
        self._camera = None

    def _update_camera_frame(self):
        if not self._cam_active or self._camera is None:
            return
        frame, dets = self._camera.get_frame()
        if frame is not None:
            import cv2
            from PIL import Image, ImageTk
            for (x1, y1, x2, y2, label, conf) in dets:
                cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
                cv2.putText(frame, f"{label} {conf:.2f}", (int(x1), max(12, int(y1) - 6)),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            self._cam_native_w, self._cam_native_h = img.width, img.height
            cw = self.cam_canvas.winfo_width() or img.width
            ch = self.cam_canvas.winfo_height() or img.height
            img = img.resize((max(cw, 1), max(ch, 1)))
            self._cam_photo = ImageTk.PhotoImage(img)
            self.cam_canvas.delete("all")
            self.cam_canvas.create_image(0, 0, anchor="nw", image=self._cam_photo)
        self.root.after(66, self._update_camera_frame)   # ~15 fps UI refresh

    def _enable_click_to_fly(self):
        self._click_to_fly_enabled = True
        self._set_status("Mapping complete — click the camera feed to fly to that GPS point", LOG_INFO)
        self.cam_canvas.focus_set()   # so U/D altitude keys are picked up right away

    def _on_camera_click(self, event):
        self.cam_canvas.focus_set()   # keep keyboard focus on the feed for U/D altitude keys
        if not self._click_to_fly_enabled or self._conn is None:
            return
        cw = self.cam_canvas.winfo_width() or 1
        ch = self.cam_canvas.winfo_height() or 1
        native_w = self._cam_native_w or cw
        native_h = self._cam_native_h or ch
        # Scale the click from the (possibly resized) display canvas back to
        # the camera's native resolution — the localization math (and the
        # calibrated PIXELS_PER_METER constants) assume native pixel coords.
        px = event.x * (native_w / cw)
        py = event.y * (native_h / ch)
        self._log(f"[CLICK] pixel=({px:.0f},{py:.0f}) of {native_w}x{native_h}", "info")
        from flight import fly_to_clicked_point
        threading.Thread(target=fly_to_clicked_point,
                         args=(self._conn, px, py, native_w, native_h),
                         daemon=True).start()

    def _on_alt_key(self, event):
        if not self._click_to_fly_enabled or self._conn is None:
            return
        key = event.keysym.lower()
        if key == 'd':
            down_m = config.CLICK_ALT_STEP_M     # D = descend (+z is "down" in NED)
        elif key == 'u':
            down_m = -config.CLICK_ALT_STEP_M    # U = climb
        else:
            return
        self._log(f"[ALT] {'Descend' if down_m > 0 else 'Climb'} {abs(down_m):.1f} m", "info")
        from flight import nudge_body
        threading.Thread(target=nudge_body, args=(self._conn, 0.0, 0.0, down_m), daemon=True).start()

    # ════════════════════════════════════════════════════════════
    #  MISSION EXECUTION
    # ════════════════════════════════════════════════════════════

    def _confirm(self):
        pts = []
        for i, (le, lo, la, _) in enumerate(self.rows, 1):
            ls, os_ = le.get().strip(), lo.get().strip()
            if not ls and not os_: continue
            try: lat, lon = float(ls), float(os_)
            except ValueError:
                messagebox.showerror("Error", f"WP {i}: bad lat/lon."); return
            if not (-90<=lat<=90 and -180<=lon<=180):
                messagebox.showerror("Error", f"WP {i}: out of range."); return
            als = la.get().strip()
            try: alt = float(als) if als else config.MISSION_ALT
            except ValueError:
                messagebox.showerror("Error", f"WP {i}: bad altitude."); return
            pts.append((lat, lon, alt))

        if not pts:
            messagebox.showwarning("No waypoints","Enter at least one waypoint."); return

        uri = self.uri_var.get().strip()
        if not uri:
            messagebox.showerror("Error","URI cannot be empty."); return

        corners = self._parse_search()
        if corners == "ERR": return

        mode = "SIMULATION" if config.TEST_FLAG else "REAL DRONE"
        msg = (f"Mode      : {mode}\n"
               f"Waypoints : {len(pts)}\n"
               f"Laps      : {self.laps_var.get()}\n"
               f"Search    : {'4-corner area' if corners else 'SKIPPED'}\n"
               f"URI       : {uri}\n\nStart mission?")
        if not messagebox.askyesno("Confirm", msg): return

        params = MissionParams(pts, self.laps_var.get(), uri, corners, True)
        self._start_btn.configure(state="disabled")
        self._set_status("Running…", LOG_WARN)
        self._mission_thd = threading.Thread(target=self._run, args=(params,), daemon=True)
        self._mission_thd.start()

    def _show_cont(self):
        self._cont_btn.pack(side="right", padx=(0, 8), before=self._start_btn)
        self._set_status("Verify WPs in Mission Planner → click Continue →", LOG_INFO)

    def _hide_cont(self):
        try: self._cont_btn.pack_forget()
        except Exception: pass

    def _user_continue(self):
        self._hide_cont()
        self._continue_ev.set()

    def _abort(self):
        if not messagebox.askyesno("Abort","Command RTL and abort?"): return
        self._log("[ABORT] RTL commanded.", "warn")
        self._click_to_fly_enabled = False
        conn = self._conn
        if conn:
            def _do_rtl():
                try:
                    from flight import rtl_and_land
                    rtl_and_land(conn)
                except Exception as e:
                    self._log(f"[ABORT] RTL error: {e}", "error")
                finally:
                    self._conn = None
                    self._stop_camera()
                    self.root.after(0, lambda: self._start_btn.configure(state="normal"))
            threading.Thread(target=_do_rtl, daemon=True).start()
        self._abort_btn.configure(state="disabled")
        self._set_status("Aborted — RTL", LOG_WARN)

    def _ask_post_lap_choice(self, search_available: bool) -> str:
        """Blocks the mission thread; builds the dialog on the main thread.
        Returns 'home' or 'search'."""
        result = {"choice": "home"}
        ev = threading.Event()

        def _build():
            dlg = ctk.CTkToplevel(self.root)
            dlg.title("Laps complete")
            dlg.geometry("420x170")
            dlg.configure(fg_color=CARD)
            dlg.grab_set()
            dlg.transient(self.root)

            _label(dlg, "Laps complete — what next?", fs=14, bold=True).pack(pady=(22, 6))
            if not search_available:
                _label(dlg, "(No search area configured — Return Home only.)",
                       fs=10, tc=MUTED).pack()

            row = ctk.CTkFrame(dlg, fg_color="transparent")
            row.pack(pady=14)

            def pick(val):
                result["choice"] = val
                dlg.destroy()
                ev.set()

            _btn(row, "Return Home (RTL)", w=180, h=40, fg=NEUTRAL, hv=NEUTRAL_H,
                 bold=True, cmd=lambda: pick("home")).pack(side="left", padx=8)
            search_btn = _btn(row, "Go to Search Area", w=180, h=40, fg=PRIMARY, hv=PRIMARY_H,
                              bold=True, cmd=lambda: pick("search"))
            search_btn.pack(side="left", padx=8)
            if not search_available:
                search_btn.configure(state="disabled")

            dlg.protocol("WM_DELETE_WINDOW", lambda: pick("home"))

        self.root.after(0, _build)
        ev.wait()
        return result["choice"]

    def _run(self, params: MissionParams):
        import config as cfg
        from connection import connect, wait_gps
        from flight import arm, fly_to, rtl_and_land, set_fixed_home, set_mode, set_param, takeoff
        from mission import WP_FILE, build_items, save_waypoints_file, upload_mission

        armed = False
        manual_handoff = False
        try:
            # Phase 1 — save preview waypoints
            p0 = params.waypoints[0]
            items = build_items(p0[0], p0[1], params.waypoints, params.laps,
                                home_lat=p0[0], home_lon=p0[1])
            save_waypoints_file(items)
            self._log(f"✓ {WP_FILE} saved — load in MP PLAN tab to verify.", "ok")
            self._log("After verifying waypoints in Mission Planner, click Continue →", "info")
            self.root.after(0, self._show_cont)
            self._continue_ev.clear()
            self._continue_ev.wait()

            # Phase 2 — connect
            self._log(f"[CONN] Connecting → {params.uri}", "info")
            self._set_status("Connecting…", LOG_INFO)
            conn = connect(uri=params.uri)
            self._conn = conn
            take_lat, take_lon = wait_gps(conn, simulation=bool(cfg.TEST_FLAG))
            msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
            alt_msl = (msg.alt/1000.0) if msg else 0.0
            home_lat = cfg.HOME_LAT or take_lat
            home_lon = cfg.HOME_LON or take_lon
            home_alt = cfg.HOME_ALT_MSL or alt_msl
            self._log(f"[MAIN] Takeoff: {take_lat:.8f}, {take_lon:.8f}", "info")
            self._log(f"[MAIN] HOME   : {home_lat:.8f}, {home_lon:.8f}", "info")

            items = build_items(take_lat, take_lon, params.waypoints, params.laps,
                                home_lat=home_lat, home_lon=home_lon)
            save_waypoints_file(items)
            set_param(conn, "RTL_ALT", cfg.MISSION_ALT * 100)
            upload_mission(conn, items)
            self._log("✓ Mission uploaded.", "ok")

            # Phase 3 — fly
            set_mode(conn, "GUIDED")
            arm(conn); armed = True
            self.root.after(0, lambda: self._abort_btn.configure(state="normal"))
            self._set_status("Flying", LOG_OK)
            takeoff(conn, cfg.MISSION_ALT)
            set_fixed_home(conn, home_lat, home_lon, home_alt)

            for lap in range(1, params.laps+1):
                self._log(f"[MAIN] ── Lap {lap}/{params.laps} ──", "info")
                prev = cfg.MISSION_ALT
                for i, wp in enumerate(params.waypoints, 1):
                    lat, lon, alt = wp
                    if abs(alt-prev) > 0.5:
                        self._log(f"[MAIN] Alt {prev:.1f}→{alt:.1f} m", "info")
                        if not fly_to(conn, lat, lon, alt):
                            raise TimeoutError(f"Alt-adjust timeout lap {lap} WP {i}")
                    if not fly_to(conn, lat, lon, alt):
                        raise TimeoutError(f"Timeout lap {lap} WP {i}")
                    prev = alt

            # Phase 4 — ask: home or search?
            choice = self._ask_post_lap_choice(search_available=bool(params.search_corners))

            if choice == "search" and params.search_corners:
                from mission import build_straight_line_path
                self._log("[SEARCH] Building straight-line pass along the longest side...", "info")
                corners_latlon = [(c[0], c[1]) for c in params.search_corners]
                search_alt = params.search_corners[0][2]
                path = build_straight_line_path(corners_latlon, alt=search_alt)
                self._log(f"[SEARCH] {len(path)}-point straight-line pass generated.", "info")

                self._log("[SEARCH] Starting camera feed...", "info")
                threading.Thread(target=self._start_camera, daemon=True).start()

                self._log("[MAIN] ── Search / mapping ──", "info")
                for i, (lat, lon, alt) in enumerate(path, 1):
                    self._log(f"[SEARCH] Leg {i}/{len(path)}", "info")
                    if not fly_to(conn, lat, lon, alt):
                        raise TimeoutError(f"Search leg {i} timeout")
                self._log("[SEARCH] Mapping complete ✓", "ok")

                self.root.after(0, self._enable_click_to_fly)
                self._log("[MAIN] Click the Camera Feed tab to fly to a marked object's real "
                          "GPS position. Use RTL / Abort when ready to return home.", "info")
                self._set_status("Manual visual approach — click camera feed, or RTL when done", LOG_INFO)
                # Mission stays in GUIDED here for manual click-to-fly control;
                # release-mechanism controls (water bottle / beacon) are not
                # implemented yet — that's a later step.
                manual_handoff = True
                return

            rtl_and_land(conn, home_lat, home_lon)
            self._log("[MAIN] Mission complete ✓", "ok")
            self._set_status("Mission complete ✓", LOG_OK)

        except Exception as e:
            self._log(f"[MAIN] Error: {e}", "error")
            self._set_status(f"Error: {e}", LOG_ERR)
            if armed and self._conn:
                self._log("[MAIN] RTL for safety.", "warn")
                try:
                    from flight import rtl_and_land
                    rtl_and_land(self._conn)
                except Exception as re:
                    self._log(f"[MAIN] RTL failed: {re}", "error")
        finally:
            self.root.after(0, self._hide_cont)
            if not manual_handoff:
                # Normal end of mission (completed or errored) — full teardown.
                self._conn = None
                self._stop_camera()
                self.root.after(0, lambda: self._start_btn.configure(state="normal"))
                self.root.after(0, lambda: self._abort_btn.configure(state="disabled"))
            # else: handed off to manual click-to-fly — keep conn, camera and
            # the RTL/Abort button live; user ends the phase via RTL/Abort.


# ── Entry points ──────────────────────────────────────────────────

def launch_gui():
    MissionGUI()

def get_mission_params():
    raise RuntimeError("Deprecated — use launch_gui() from main.py.")