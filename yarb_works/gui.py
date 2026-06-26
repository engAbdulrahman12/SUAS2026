"""SUAS 2026 — Mission Planning GUI.

Two tabs:
  • Mission  — lap waypoints loaded from JSON or typed manually.
  • Search   — two corner coordinates defining the rectangle search area.
               The drone flies corner-1 → corner-2 in a straight line, then RTL.
"""
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import dataclass
import config


@dataclass
class MissionParams:
    waypoints:    list   # [(lat, lon, alt), ...]
    laps:         int
    uri:          str
    search_start: tuple  # (lat, lon, alt) or None
    search_end:   tuple  # (lat, lon, alt) or None
    confirmed:    bool = False


class MissionGUI:
    def __init__(self):
        self.result = MissionParams([], 0, config.default_uri(), None, None, False)
        self.rows   = []
        self.root   = tk.Tk()
        self.root.title("SUAS 2026 — Mission Planner")
        self.root.geometry("920x640")
        self._build()
        self.root.mainloop()

    # ── Top banner + connection bar ───────────────────────────

    def _build(self):
        mode     = "SIMULATION" if config.TEST_FLAG else "REAL DRONE"
        home_str = (f"{config.HOME_LAT:.7f}, {config.HOME_LON:.7f}"
                    if config.HOME_LAT is not None else "AUTO from GPS")
        banner   = f"{mode}  |  Default alt: {config.MISSION_ALT:.1f} m AGL  |  Home: {home_str}"
        tk.Label(self.root, text=banner, bg="#2563eb", fg="white",
                 font=("Segoe UI", 10, "bold"), pady=8).pack(fill="x")

        # Connection bar
        conn = tk.LabelFrame(self.root, text="Connection", padx=8, pady=4)
        conn.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(conn, text="Port / URI:").pack(side="left")
        self.uri_var = tk.StringVar(value=config.default_uri())
        tk.Entry(conn, textvariable=self.uri_var, width=26).pack(side="left", padx=4)
        for label, uri in [("SITL", "tcp:127.0.0.1:5762"),
                            ("COM6", "com:COM6"), ("COM3", "com:COM3"),
                            ("UDP",  "udp:0.0.0.0:14550")]:
            tk.Button(conn, text=label, width=5,
                      command=lambda u=uri: self.uri_var.set(u)).pack(side="left", padx=2)
        tk.Label(conn, text="  ⚠ Disconnect Mission Planner from same COM port before flying.",
                 fg="#b45309", font=("Segoe UI", 9)).pack(side="left", padx=6)

        # Tabs
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=6)
        self._build_mission_tab(nb)
        self._build_search_tab(nb)

        # Bottom buttons
        btns = tk.Frame(self.root, pady=6)
        btns.pack(fill="x", padx=10)
        tk.Button(btns, text="Cancel", command=self.root.destroy).pack(side="right", padx=4)
        tk.Button(btns, text="▶  Start Mission", command=self._confirm,
                  bg="#22c55e", fg="white",
                  font=("Segoe UI", 10, "bold"), padx=10).pack(side="right")

    # ── Mission tab ───────────────────────────────────────────

    def _build_mission_tab(self, nb):
        frame = tk.Frame(nb)
        nb.add(frame, text="  Mission Waypoints  ")

        top = tk.Frame(frame, pady=6)
        top.pack(fill="x")
        tk.Button(top, text="📂 Load JSON", command=self._load_json).pack(side="left")
        tk.Label(top, text="    Laps:").pack(side="left")
        self.laps_var = tk.IntVar(value=config.DEFAULT_LAPS)
        tk.Spinbox(top, from_=1, to=20, textvariable=self.laps_var, width=5).pack(side="left")
        tk.Button(top, text="+ Add WP",    command=self._add_row).pack(side="left", padx=8)
        tk.Button(top, text="Remove Last", command=self._remove_last).pack(side="left")
        tk.Button(top, text="Clear All",   command=self._clear).pack(side="left", padx=4)

        hdr = tk.Frame(frame)
        hdr.pack(fill="x")
        for col, (text, w) in enumerate([("#", 4), ("Latitude", 20), ("Longitude", 20),
                                          ("Alt AGL (m)", 12), ("Name", 14)]):
            tk.Label(hdr, text=text, width=w, anchor="w",
                     font=("Segoe UI", 9, "bold")).grid(row=0, column=col, padx=2)

        outer  = tk.Frame(frame)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, borderwidth=0)
        vsb    = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.table = tk.Frame(canvas)
        self.table.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.table, anchor="nw")

        for _ in range(4):
            self._add_row()

    def _add_row(self, lat="", lon="", alt="", name=""):
        idx   = len(self.rows) + 1
        tk.Label(self.table, text=str(idx), width=4, anchor="w").grid(row=idx, column=0, pady=1)
        lat_e  = tk.Entry(self.table, width=20); lat_e.insert(0, lat)
        lon_e  = tk.Entry(self.table, width=20); lon_e.insert(0, lon)
        alt_e  = tk.Entry(self.table, width=12); alt_e.insert(0, alt)
        name_e = tk.Entry(self.table, width=14); name_e.insert(0, name)
        for col, w in enumerate([lat_e, lon_e, alt_e, name_e], 1):
            w.grid(row=idx, column=col, padx=2, pady=1)
        self.rows.append((lat_e, lon_e, alt_e, name_e))

    def _remove_last(self):
        if not self.rows:
            return
        entries = self.rows.pop()
        row = int(entries[0].grid_info()["row"])
        for w in self.table.grid_slaves(row=row):
            w.destroy()

    def _clear(self):
        for entries in self.rows:
            row = int(entries[0].grid_info()["row"])
            for w in self.table.grid_slaves(row=row):
                w.destroy()
        self.rows.clear()

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
            # Load search corners if present in the JSON
            if "search_start" in data:
                s = data["search_start"]
                self.s_lat1.delete(0, tk.END); self.s_lat1.insert(0, str(s.get("lat","")))
                self.s_lon1.delete(0, tk.END); self.s_lon1.insert(0, str(s.get("lon","")))
                self.s_alt1.delete(0, tk.END); self.s_alt1.insert(0, str(s.get("alt","")))
            if "search_end" in data:
                e = data["search_end"]
                self.s_lat2.delete(0, tk.END); self.s_lat2.insert(0, str(e.get("lat","")))
                self.s_lon2.delete(0, tk.END); self.s_lon2.insert(0, str(e.get("lon","")))
                self.s_alt2.delete(0, tk.END); self.s_alt2.insert(0, str(e.get("alt","")))
            messagebox.showinfo("Loaded", f"Loaded {len(wps)} waypoints from:\n{path}")
        except Exception as e:
            messagebox.showerror("Load error", str(e))

    # ── Search tab ────────────────────────────────────────────

    def _build_search_tab(self, nb):
        frame = tk.Frame(nb)
        nb.add(frame, text="  Search Area  ")

        tk.Label(frame,
                 text="Set S = midpoint of the entry side, E = midpoint of the exit side.\n"
                      "The drone flies straight from S through the center to E, then RTL.\n"
                      "Leave both blank to skip the search.",
                 justify="left", fg="#374151", pady=8).pack(anchor="w", padx=16)

        # Visual diagram
        diag = tk.Canvas(frame, width=300, height=130, bg="#f3f4f6", highlightthickness=1,
                         highlightbackground="#d1d5db")
        diag.pack(padx=16, pady=(0, 10), anchor="w")
        diag.create_rectangle(40, 25, 260, 105, outline="#6b7280", width=2)
        diag.create_oval(30, 57, 50, 77, fill="#22c55e", outline="")
        diag.create_text(18, 50, text="S", font=("Segoe UI", 11, "bold"), fill="#059669")
        diag.create_oval(250, 57, 270, 77, fill="#ef4444", outline="")
        diag.create_text(278, 50, text="E", font=("Segoe UI", 11, "bold"), fill="#dc2626")
        diag.create_line(40, 67, 260, 67, fill="#3b82f6", width=3, arrow=tk.LAST)
        diag.create_text(150, 118, text="S and E are midpoints of the short sides",
                         font=("Segoe UI", 8), fill="#374151")

        # Input grid
        grid = tk.Frame(frame, padx=16)
        grid.pack(anchor="w")

        headers = ["", "Latitude", "Longitude", "Alt AGL (m)"]
        for col, h in enumerate(headers):
            tk.Label(grid, text=h, font=("Segoe UI", 9, "bold"),
                     width=14, anchor="w").grid(row=0, column=col, padx=4, pady=2)

        tk.Label(grid, text="S — Entry midpoint", anchor="w", width=14,
                 fg="#059669").grid(row=1, column=0, padx=4, pady=4)
        self.s_lat1 = tk.Entry(grid, width=14); self.s_lat1.grid(row=1, column=1, padx=4)
        self.s_lon1 = tk.Entry(grid, width=14); self.s_lon1.grid(row=1, column=2, padx=4)
        self.s_alt1 = tk.Entry(grid, width=10); self.s_alt1.grid(row=1, column=3, padx=4)

        tk.Label(grid, text="E — Exit midpoint", anchor="w", width=14,
                 fg="#dc2626").grid(row=2, column=0, padx=4, pady=4)
        self.s_lat2 = tk.Entry(grid, width=14); self.s_lat2.grid(row=2, column=1, padx=4)
        self.s_lon2 = tk.Entry(grid, width=14); self.s_lon2.grid(row=2, column=2, padx=4)
        self.s_alt2 = tk.Entry(grid, width=10); self.s_alt2.grid(row=2, column=3, padx=4)

        tk.Label(grid, text="(blank alt → uses Mission Alt from config)",
                 fg="gray", font=("Segoe UI", 8)).grid(row=3, column=1, columnspan=3,
                                                        sticky="w", padx=4)

    def _parse_search(self):
        """Return (start, end) tuples or (None, None) if search is skipped."""
        l1 = self.s_lat1.get().strip()
        o1 = self.s_lon1.get().strip()
        l2 = self.s_lat2.get().strip()
        o2 = self.s_lon2.get().strip()

        if not any([l1, o1, l2, o2]):
            return None, None   # search skipped

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
        # Validate mission waypoints
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

        # Validate search area
        s_start, s_end = self._parse_search()
        if s_start == "ERROR":
            return

        # Confirm dialog
        search_line = ("straight line: corner1 → corner2"
                       if s_start else "SKIPPED")
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
