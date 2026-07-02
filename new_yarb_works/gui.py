"""SUAS 2026 — Mission Planning GUI.

Two tabs:
  • Mission  — lap waypoints loaded from JSON or typed manually.
  • Search   — two corner coordinates defining the rectangle search area.
               The drone flies corner-1 → corner-2 in a straight line, then RTL.

Built on CustomTkinter (not raw ttk): widgets are rasterized as anti-aliased
images rather than native OS primitives, so corners/hover states render
properly, and it handles Windows DPI awareness internally — fixes the
blurry/pixelated scaling you get from plain tkinter on >100% display scaling.
"""
import json
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from dataclasses import dataclass
import config

ctk.set_appearance_mode("dark")

# ── Design tokens ────────────────────────────────────────────────
FONT        = "Segoe UI"
BG          = "#15181f"
CARD        = "#1c2029"
CARD_ALT    = "#20242e"
BORDER      = "#2a2f3a"
TEXT        = "#e5e7eb"
TEXT_MUTED  = "#8b92a3"
PRIMARY     = "#4f46e5"
PRIMARY_HV  = "#4338ca"
SUCCESS     = "#16a34a"
SUCCESS_HV  = "#15803d"
NEUTRAL     = "#2a2f3a"
NEUTRAL_HV  = "#353b48"
ACCENT_S    = "#10b981"
ACCENT_E    = "#ef4444"


@dataclass
class MissionParams:
    waypoints:    list   # [(lat, lon, alt), ...]
    laps:         int
    uri:          str
    search_start: tuple  # (lat, lon, alt) or None
    search_end:   tuple  # (lat, lon, alt) or None
    confirmed:    bool = False


def _entry(parent, width=180, **kw):
    return ctk.CTkEntry(parent, width=width, height=32, corner_radius=6,
                        fg_color=CARD_ALT, border_color=BORDER, border_width=1,
                        text_color=TEXT, font=(FONT, 12), **kw)


class MissionGUI:
    def __init__(self):
        self.result = MissionParams([], 0, config.default_uri(), None, None, False)
        self.rows        = []   # list of (lat_e, lon_e, alt_e, name_e)
        self.row_frames  = []

        self.root = ctk.CTk()
        self.root.title("SUAS 2026 — Mission Planner")
        self.root.geometry("1000x700")
        self.root.configure(fg_color=BG)
        self._build()
        self.root.mainloop()

    # ── Layout root ────────────────────────────────────────────

    def _build(self):
        self._build_header()

        body = ctk.CTkFrame(self.root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=16)

        self._build_connection_card(body)

        self.tabs = ctk.CTkTabview(
            body, fg_color=CARD, segmented_button_fg_color=CARD_ALT,
            segmented_button_selected_color=PRIMARY,
            segmented_button_selected_hover_color=PRIMARY_HV,
            segmented_button_unselected_color=CARD_ALT,
            text_color=TEXT, corner_radius=10)
        self.tabs.pack(fill="both", expand=True, pady=(14, 0))
        self.tabs.add("Mission Waypoints")
        self.tabs.add("Search Area")
        self._build_mission_tab(self.tabs.tab("Mission Waypoints"))
        self._build_search_tab(self.tabs.tab("Search Area"))

        self._build_footer(body)

    def _build_header(self):
        header = ctk.CTkFrame(self.root, fg_color="#0d0f14", corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=22)

        left = ctk.CTkFrame(inner, fg_color="transparent")
        left.pack(side="left", fill="y")
        ctk.CTkLabel(left, text="SUAS 2026", text_color="white",
                    font=(FONT, 16, "bold")).pack(side="left", pady=16)
        ctk.CTkLabel(left, text="  Mission Planner", text_color=TEXT_MUTED,
                    font=(FONT, 13)).pack(side="left", pady=16)

        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="right", fill="y")

        is_sim = bool(config.TEST_FLAG)
        badge_bg  = "#1e3a5f" if is_sim else "#5f2e1e"
        badge_fg  = "#7dd3fc" if is_sim else "#fdba74"
        mode_txt  = "● SIMULATION" if is_sim else "● REAL DRONE"

        home_str = (f"{config.HOME_LAT:.5f}, {config.HOME_LON:.5f}"
                    if config.HOME_LAT is not None else "auto (GPS)")
        ctk.CTkLabel(right, text=f"Alt {config.MISSION_ALT:.1f} m AGL   ·   Home: {home_str}",
                    text_color=TEXT_MUTED, font=(FONT, 11)).pack(side="left", padx=(0, 14))

        badge = ctk.CTkLabel(right, text=mode_txt, text_color=badge_fg, fg_color=badge_bg,
                             corner_radius=6, font=(FONT, 11, "bold"), padx=10, pady=4)
        badge.pack(side="left")

    def _build_connection_card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        card.pack(fill="x")
        pad = ctk.CTkFrame(card, fg_color="transparent")
        pad.pack(fill="x", padx=18, pady=14)

        ctk.CTkLabel(pad, text="CONNECTION", text_color=TEXT_MUTED,
                    font=(FONT, 10, "bold")).grid(row=0, column=0, columnspan=6,
                                                   sticky="w", pady=(0, 10))

        ctk.CTkLabel(pad, text="URI", text_color=TEXT, font=(FONT, 12)).grid(
            row=1, column=0, sticky="w", padx=(0, 8))
        self.uri_var = tk.StringVar(value=config.default_uri())
        _entry(pad, width=230, textvariable=self.uri_var).grid(
            row=1, column=1, padx=(0, 16))

        for i, (label, uri) in enumerate([("SITL", "tcp:127.0.0.1:5762"),
                                           ("Drone", "udp:0.0.0.0:14552"),
                                           ("COM6", "COM6"), ("COM3", "COM3")]):
            ctk.CTkButton(pad, text=label, width=56, height=28, corner_radius=6,
                         fg_color=NEUTRAL, hover_color=NEUTRAL_HV, text_color=TEXT,
                         font=(FONT, 10, "bold"),
                         command=lambda u=uri: self.uri_var.set(u)).grid(
                row=1, column=2 + i, padx=3)

        ctk.CTkLabel(pad, text="ℹ  Run start_mavproxy.bat first, then point Mission Planner at UDP:14550",
                    text_color=TEXT_MUTED, font=(FONT, 10)).grid(
            row=2, column=0, columnspan=7, sticky="w", pady=(10, 0))

    def _build_footer(self, parent):
        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", pady=(14, 0))
        ctk.CTkButton(btns, text="▶  Start Mission", height=42, corner_radius=8,
                     fg_color=SUCCESS, hover_color=SUCCESS_HV, text_color="white",
                     font=(FONT, 13, "bold"), command=self._confirm).pack(
            side="right")
        ctk.CTkButton(btns, text="Cancel", height=42, width=90, corner_radius=8,
                     fg_color="transparent", hover_color=NEUTRAL, text_color=TEXT_MUTED,
                     border_width=1, border_color=BORDER, font=(FONT, 12),
                     command=self.root.destroy).pack(side="right", padx=(0, 10))

    # ── Mission tab ───────────────────────────────────────────

    def _build_mission_tab(self, tab):
        tab.configure(fg_color="transparent")

        toolbar = ctk.CTkFrame(tab, fg_color=CARD_ALT, corner_radius=8)
        toolbar.pack(fill="x", padx=4, pady=(4, 10))
        tb = ctk.CTkFrame(toolbar, fg_color="transparent")
        tb.pack(fill="x", padx=12, pady=10)

        ctk.CTkButton(tb, text="📂  Load JSON", height=32, corner_radius=6,
                     fg_color=PRIMARY, hover_color=PRIMARY_HV, font=(FONT, 12, "bold"),
                     command=self._load_json).pack(side="left")

        ctk.CTkFrame(tb, fg_color=BORDER, width=1, height=24).pack(
            side="left", padx=16)

        ctk.CTkLabel(tb, text="Laps", text_color=TEXT, font=(FONT, 12)).pack(side="left")
        self.laps_var = tk.IntVar(value=config.DEFAULT_LAPS)
        laps_lbl = ctk.CTkLabel(tb, textvariable=self.laps_var, width=28, height=28,
                                fg_color=CARD, corner_radius=6, text_color=TEXT,
                                font=(FONT, 12, "bold"))
        laps_lbl.pack(side="left", padx=8)
        ctk.CTkButton(tb, text="−", width=28, height=28, corner_radius=6,
                     fg_color=NEUTRAL, hover_color=NEUTRAL_HV,
                     command=lambda: self.laps_var.set(max(1, self.laps_var.get() - 1))
                     ).pack(side="left", padx=(0, 2))
        ctk.CTkButton(tb, text="+", width=28, height=28, corner_radius=6,
                     fg_color=NEUTRAL, hover_color=NEUTRAL_HV,
                     command=lambda: self.laps_var.set(min(20, self.laps_var.get() + 1))
                     ).pack(side="left", padx=(0, 16))

        ctk.CTkButton(tb, text="+ Add Row", height=32, corner_radius=6,
                     fg_color=NEUTRAL, hover_color=NEUTRAL_HV, font=(FONT, 11),
                     command=self._add_row).pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Remove Last", height=32, corner_radius=6,
                     fg_color=NEUTRAL, hover_color=NEUTRAL_HV, font=(FONT, 11),
                     command=self._remove_last).pack(side="left", padx=3)
        ctk.CTkButton(tb, text="Clear All", height=32, corner_radius=6,
                     fg_color=NEUTRAL, hover_color=NEUTRAL_HV, font=(FONT, 11),
                     command=self._clear).pack(side="left", padx=3)

        table_card = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        table_card.pack(fill="both", expand=True, padx=4)

        hdr = ctk.CTkFrame(table_card, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        for col, (text, w) in enumerate([("#", 30), ("LATITUDE", 190), ("LONGITUDE", 190),
                                          ("ALT AGL (m)", 110), ("NAME", 140)]):
            ctk.CTkLabel(hdr, text=text, width=w, anchor="w", text_color=TEXT_MUTED,
                        font=(FONT, 10, "bold")).grid(row=0, column=col, padx=4)

        self.scroll = ctk.CTkScrollableFrame(table_card, fg_color="transparent",
                                             scrollbar_button_color=NEUTRAL,
                                             scrollbar_button_hover_color=NEUTRAL_HV)
        self.scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for _ in range(4):
            self._add_row()

    def _add_row(self, lat="", lon="", alt="", name=""):
        idx = len(self.rows) + 1
        row_bg = CARD if idx % 2 else CARD_ALT
        row = ctk.CTkFrame(self.scroll, fg_color=row_bg, corner_radius=6)
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(row, text=str(idx), width=30, text_color=TEXT_MUTED,
                    font=(FONT, 10, "bold")).grid(row=0, column=0, padx=(6, 4), pady=6)
        lat_e  = _entry(row, width=190); lat_e.insert(0, lat)
        lon_e  = _entry(row, width=190); lon_e.insert(0, lon)
        alt_e  = _entry(row, width=110); alt_e.insert(0, alt)
        name_e = _entry(row, width=140); name_e.insert(0, name)
        for col, w in enumerate([lat_e, lon_e, alt_e, name_e], 1):
            w.grid(row=0, column=col, padx=4, pady=6)

        self.rows.append((lat_e, lon_e, alt_e, name_e))
        self.row_frames.append(row)

    def _remove_last(self):
        if not self.rows:
            return
        self.rows.pop()
        self.row_frames.pop().destroy()

    def _clear(self):
        for row in self.row_frames:
            row.destroy()
        self.rows.clear()
        self.row_frames.clear()

    def _load_json(self):
        path = filedialog.askopenfilename(title="Select waypoints JSON",
                                          filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            data = json.load(open(path, encoding="utf-8"))
            wps  = data.get("waypoints", [])
            if not wps:
                messagebox.showwarning("Empty", "No 'waypoints' key found.")
                return
            if "default_laps" in data:
                self.laps_var.set(int(data["default_laps"]))
            self._clear()
            for wp in wps:
                self._add_row(str(wp.get("lat", "")), str(wp.get("lon", "")),
                              str(wp.get("alt", "")), str(wp.get("name", "")))
            if "search_start" in data:
                s = data["search_start"]
                self.s_lat1.delete(0, tk.END); self.s_lat1.insert(0, str(s.get("lat", "")))
                self.s_lon1.delete(0, tk.END); self.s_lon1.insert(0, str(s.get("lon", "")))
                self.s_alt1.delete(0, tk.END); self.s_alt1.insert(0, str(s.get("alt", "")))
            if "search_end" in data:
                e = data["search_end"]
                self.s_lat2.delete(0, tk.END); self.s_lat2.insert(0, str(e.get("lat", "")))
                self.s_lon2.delete(0, tk.END); self.s_lon2.insert(0, str(e.get("lon", "")))
                self.s_alt2.delete(0, tk.END); self.s_alt2.insert(0, str(e.get("alt", "")))
            messagebox.showinfo("Loaded", f"Loaded {len(wps)} waypoints from:\n{path}")
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    # ── Search tab ────────────────────────────────────────────

    def _build_search_tab(self, tab):
        tab.configure(fg_color="transparent")

        info = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        info.pack(fill="x", padx=4, pady=(4, 10))
        ipad = ctk.CTkFrame(info, fg_color="transparent")
        ipad.pack(fill="x", padx=18, pady=14)
        ctk.CTkLabel(ipad, text="Set S = midpoint of the entry side, E = midpoint of the exit side.",
                    text_color=TEXT, font=(FONT, 12), anchor="w").pack(fill="x")
        ctk.CTkLabel(ipad,
                    text="The drone flies straight from S through the center to E, then RTL. Leave both blank to skip.",
                    text_color=TEXT_MUTED, font=(FONT, 11), anchor="w").pack(fill="x", pady=(3, 0))

        diagram_card = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        diagram_card.pack(fill="x", padx=4, pady=(0, 10))
        dpad = ctk.CTkFrame(diagram_card, fg_color="transparent")
        dpad.pack(pady=14)
        diag = tk.Canvas(dpad, width=320, height=110, bg=CARD, highlightthickness=0)
        diag.pack()
        diag.create_rectangle(40, 20, 280, 90, outline=BORDER, width=2, fill=CARD_ALT)
        diag.create_oval(30, 45, 50, 65, fill=ACCENT_S, outline="")
        diag.create_text(18, 40, text="S", font=(FONT, 11, "bold"), fill=ACCENT_S)
        diag.create_oval(270, 45, 290, 65, fill=ACCENT_E, outline="")
        diag.create_text(300, 40, text="E", font=(FONT, 11, "bold"), fill=ACCENT_E)
        diag.create_line(40, 55, 280, 55, fill=PRIMARY, width=3, arrow=tk.LAST)
        diag.create_text(160, 100, text="S and E are midpoints of the short sides",
                         font=(FONT, 8), fill=TEXT_MUTED)

        input_card = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=10)
        input_card.pack(fill="x", padx=4)
        grid = ctk.CTkFrame(input_card, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=16)

        for col, h in enumerate(["", "LATITUDE", "LONGITUDE", "ALT AGL (m)"]):
            ctk.CTkLabel(grid, text=h, font=(FONT, 10, "bold"), text_color=TEXT_MUTED,
                        width=140, anchor="w").grid(row=0, column=col, padx=4, pady=(0, 8))

        ctk.CTkLabel(grid, text="●  S — Entry", text_color=ACCENT_S, width=140,
                    anchor="w", font=(FONT, 12, "bold")).grid(row=1, column=0, padx=4)
        self.s_lat1 = _entry(grid, width=170); self.s_lat1.grid(row=1, column=1, padx=4, pady=5)
        self.s_lon1 = _entry(grid, width=170); self.s_lon1.grid(row=1, column=2, padx=4, pady=5)
        self.s_alt1 = _entry(grid, width=110); self.s_alt1.grid(row=1, column=3, padx=4, pady=5)

        ctk.CTkLabel(grid, text="●  E — Exit", text_color=ACCENT_E, width=140,
                    anchor="w", font=(FONT, 12, "bold")).grid(row=2, column=0, padx=4)
        self.s_lat2 = _entry(grid, width=170); self.s_lat2.grid(row=2, column=1, padx=4, pady=5)
        self.s_lon2 = _entry(grid, width=170); self.s_lon2.grid(row=2, column=2, padx=4, pady=5)
        self.s_alt2 = _entry(grid, width=110); self.s_alt2.grid(row=2, column=3, padx=4, pady=5)

        ctk.CTkLabel(grid, text="Leave altitude blank to use Mission Alt from config.",
                    text_color=TEXT_MUTED, font=(FONT, 10)).grid(
            row=3, column=1, columnspan=3, sticky="w", padx=4, pady=(8, 0))

    def _parse_search(self):
        l1 = self.s_lat1.get().strip()
        o1 = self.s_lon1.get().strip()
        l2 = self.s_lat2.get().strip()
        o2 = self.s_lon2.get().strip()

        if not any([l1, o1, l2, o2]):
            return None, None

        if not all([l1, o1, l2, o2]):
            messagebox.showerror("Search Error",
                                 "Fill in both corners (lat + lon) or leave both completely blank.")
            return "ERROR", "ERROR"

        try:
            lat1, lon1 = float(l1), float(o1)
            lat2, lon2 = float(l2), float(o2)
        except ValueError:
            messagebox.showerror("Search Error", "Invalid coordinates in search area.")
            return "ERROR", "ERROR"

        a1 = self.s_alt1.get().strip()
        a2 = self.s_alt2.get().strip()
        try:
            alt1 = float(a1) if a1 else config.MISSION_ALT
            alt2 = float(a2) if a2 else config.MISSION_ALT
        except ValueError:
            messagebox.showerror("Search Error", "Invalid altitude in search area.")
            return "ERROR", "ERROR"

        return (lat1, lon1, alt1), (lat2, lon2, alt2)

    # ── Confirm ───────────────────────────────────────────────

    def _confirm(self):
        pts = []
        for i, (lat_e, lon_e, alt_e, _) in enumerate(self.rows, 1):
            ls, os_ = lat_e.get().strip(), lon_e.get().strip()
            if not ls and not os_:
                continue
            try:
                lat, lon = float(ls), float(os_)
            except ValueError:
                messagebox.showerror("Error", f"WP {i}: invalid lat/lon."); return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                messagebox.showerror("Error", f"WP {i}: out of range."); return
            alt_s = alt_e.get().strip()
            try:
                alt = float(alt_s) if alt_s else config.MISSION_ALT
            except ValueError:
                messagebox.showerror("Error", f"WP {i}: invalid altitude."); return
            pts.append((lat, lon, alt))

        if not pts:
            messagebox.showwarning("No waypoints", "Enter at least one mission waypoint.")
            return

        uri = self.uri_var.get().strip()
        if not uri:
            messagebox.showerror("Error", "Connection URI cannot be empty.")
            return

        s_start, s_end = self._parse_search()
        if s_start == "ERROR":
            return

        search_line = "straight line: corner1 → corner2" if s_start else "SKIPPED"
        msg = (f"Waypoints : {len(pts)}\n"
               f"Laps      : {self.laps_var.get()}\n"
               f"Search    : {search_line}\n"
               f"URI       : {uri}\n\nStart mission?")
        if messagebox.askyesno("Confirm", msg):
            self.result = MissionParams(pts, self.laps_var.get(), uri,
                                        s_start, s_end, True)
            self.root.destroy()


def get_mission_params() -> MissionParams:
    return MissionGUI().result