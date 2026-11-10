#!/usr/bin/env python3
"""GUI for scn_decomp.py: run the decompiler and browse the decompiled scenes.

  uv run --no-project --python-preference only-managed python scn_gui.py
  (or double-click scn_gui.cmd)

Double-click a function name in the code (fn_33ab9) to jump to it, Alt+Left to go back, Ctrl+F to search.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "out" / "scn_gui.json"
KEYWORDS = r"\b(?:if|else|while|for|do|return|break|continue|goto|void|int|float|s8|s16|s32|u8|static|extern|char|enum)\b"
TOKEN = re.compile(r"(?P<comment>/\*.*?\*/|//[^\n]*)|(?P<string>\"(?:[^\"\\\n]|\\.)*\")|(?P<keyword>%s)|(?P<call>\b[A-Za-z_]\w*(?=\())|"
                   r"(?P<number>\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?f?)\b)|(?P<label>^\s*L_[0-9a-f]+:)" % KEYWORDS, re.M)
FUNC_HEAD = re.compile(r"^/\* 0x([0-9a-f]+)(.*?) \*/\n([^\n]*\b(fn_[0-9a-f]+)\()", re.M)
DARK = {"bg": "#16191f", "fg": "#d8dee9", "sel": "#2e3a50", "comment": "#7f8a9a", "string": "#8fcf9c", "keyword": "#c79be0",
        "call": "#62bfd8", "number": "#e3a86b", "label": "#ef8a80", "fnlink": "#9ab4f4", "hit": "#5a4a10", "line": "#5c6676"}


class App:
    def __init__(self, root, parent=None, out_var=None, on_edit=None, on_finished=None, is_active=None):
        """parent: frame to build into (a tab of scn_studio); without it the app owns the window."""
        self.root = root
        self.box = parent or root
        self.on_edit, self.on_finished = on_edit, on_finished
        self.is_active = is_active or (lambda: True)
        if parent is None:
            root.title("SCN decompiler")
            root.geometry("1500x900")
        self.settings = self.load_settings()
        self.scn_dir = tk.StringVar(value=self.settings.get("scn_dir", str(HERE / "scn")))
        self.out_dir = out_var or tk.StringVar(value=self.settings.get("out_dir", str(HERE / "out" / "scn_c")))
        self.scene_filter = tk.StringVar()
        self.func_filter = tk.StringVar()
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="ready")
        self.proc = None
        self.q = queue.Queue()
        self.current = None
        self.funcs = []
        self.func_line = {}
        self.history = []
        self.highlighted = set()
        self.build()
        self.refresh_scenes()
        self.root.after(100, self.poll)

    # ---------------------------------------------------------------- layout
    def build(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        top = ttk.Frame(self.box, padding=6)
        top.pack(fill="x")
        for row, (label, var, pick) in enumerate((("Scenes (.scn)", self.scn_dir, self.pick_scn), ("Output (.c)", self.out_dir, self.pick_out))):
            ttk.Label(top, text=label, width=14).grid(row=row, column=0, sticky="w")
            ttk.Entry(top, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4, pady=2)
            ttk.Button(top, text="Browse…", command=pick).grid(row=row, column=2, padx=2)
        top.columnconfigure(1, weight=1)
        bar = ttk.Frame(top)
        bar.grid(row=0, column=3, rowspan=2, padx=(12, 0))
        self.btn_all = ttk.Button(bar, text="Decompile all", command=self.run_all)
        self.btn_one = ttk.Button(bar, text="Decompile selected", command=self.run_selected)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        for i, b in enumerate((self.btn_all, self.btn_one, self.btn_stop)):
            b.grid(row=0, column=i, padx=2)
        ttk.Button(bar, text="Report", command=self.show_report).grid(row=1, column=0, padx=2, pady=2, sticky="ew")
        ttk.Button(bar, text="scn.h", command=self.show_header).grid(row=1, column=1, padx=2, pady=2, sticky="ew")
        ttk.Button(bar, text="Open in VS Code", command=self.open_code).grid(row=1, column=2, padx=2, pady=2, sticky="ew")
        if self.on_edit:
            ttk.Button(bar, text="Edit in workspace →", command=self.edit).grid(row=0, column=3, rowspan=2, padx=(10, 2), sticky="ns")

        main = ttk.PanedWindow(self.box, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6)
        left = ttk.Frame(main)
        main.add(left, weight=0)
        ttk.Label(left, text="Scenes").pack(anchor="w")
        ttk.Entry(left, textvariable=self.scene_filter).pack(fill="x")
        self.scene_filter.trace_add("write", lambda *_: self.refresh_scenes())
        self.scenes = tk.Listbox(left, width=26, exportselection=False, activestyle="none")
        self.scenes.pack(fill="both", expand=True)
        self.scenes.bind("<<ListboxSelect>>", lambda e: self.open_selected())

        mid = ttk.Frame(main)
        main.add(mid, weight=0)
        ttk.Label(mid, text="Functions").pack(anchor="w")
        ttk.Entry(mid, textvariable=self.func_filter).pack(fill="x")
        self.func_filter.trace_add("write", lambda *_: self.fill_funcs())
        self.funclist = tk.Listbox(mid, width=34, exportselection=False, activestyle="none", font=("Consolas", 10))
        self.funclist.pack(fill="both", expand=True)
        self.funclist.bind("<<ListboxSelect>>", lambda e: self.goto_selected_func())

        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=1)
        codebox = ttk.Frame(right)
        right.add(codebox, weight=4)
        sb = ttk.Frame(codebox)
        sb.pack(fill="x")
        self.title = ttk.Label(sb, text="", font=("Segoe UI", 10, "bold"))
        self.title.pack(side="left")
        ttk.Button(sb, text="Next", command=self.find_next).pack(side="right")
        e = ttk.Entry(sb, textvariable=self.search, width=30)
        e.pack(side="right", padx=4)
        e.bind("<Return>", lambda ev: self.find_next())
        self.search_entry = e
        ttk.Label(sb, text="Find").pack(side="right")
        ttk.Button(sb, text="◀ Back", command=self.back).pack(side="right", padx=8)
        frame = ttk.Frame(codebox)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", font=("Consolas", 11), bg=DARK["bg"], fg=DARK["fg"], insertbackground=DARK["fg"],
                            selectbackground=DARK["sel"], undo=False, tabs=("4c",))
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.on_xscroll)
        self.text.configure(yscrollcommand=lambda a, b: (ys.set(a, b), self.highlight_visible()), xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.text.pack(fill="both", expand=True)
        for tag in ("comment", "string", "keyword", "call", "number", "label"):
            self.text.tag_configure(tag, foreground=DARK[tag])
        self.text.tag_configure("comment", font=("Consolas", 11, "italic"))
        self.text.tag_configure("keyword", font=("Consolas", 11, "bold"))
        self.text.tag_configure("fnlink", foreground=DARK["fnlink"], underline=True)
        self.text.tag_configure("hit", background=DARK["hit"])
        self.text.tag_configure("current", background="#26303f")
        self.text.tag_raise("hit")
        self.text.bind("<Double-Button-1>", self.on_double)
        self.text.bind("<Key>", self.readonly_key)
        self.text.bind("<Configure>", lambda e: self.highlight_visible())
        self.root.bind("<Control-f>", lambda e: self.is_active() and (self.search_entry.focus_set(), self.search_entry.select_range(0, "end")), add="+")
        self.root.bind("<F3>", lambda e: self.is_active() and self.find_next(), add="+")
        self.root.bind("<Alt-Left>", lambda e: self.is_active() and self.back(), add="+")

        logbox = ttk.Frame(right)
        right.add(logbox, weight=1)
        self.log = tk.Text(logbox, height=8, wrap="none", font=("Consolas", 9), bg="#0f1115", fg="#b8c2d0")
        ls = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        st = ttk.Frame(self.box, padding=(6, 2))
        st.pack(fill="x")
        self.progress = ttk.Progressbar(st, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        ttk.Label(st, textvariable=self.status).pack(side="left")

    # ---------------------------------------------------------------- settings
    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({"scn_dir": self.scn_dir.get(), "out_dir": self.out_dir.get()}))
        except OSError:
            pass

    def pick_scn(self):
        d = filedialog.askdirectory(initialdir=self.scn_dir.get(), title="Folder with .scn files")
        if d:
            self.scn_dir.set(d)
            self.save_settings()
            self.refresh_scenes()

    def pick_out(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get(), title="Output folder")
        if d:
            self.out_dir.set(d)
            self.save_settings()
            self.refresh_scenes()

    # ---------------------------------------------------------------- scenes and functions
    def scene_names(self):
        names = {p.stem for p in Path(self.scn_dir.get()).glob("*.scn")}
        names |= {p.stem for p in Path(self.out_dir.get()).glob("*.c")}
        return sorted(names)

    def refresh_scenes(self):
        keep = self.current
        flt = self.scene_filter.get().lower()
        out = Path(self.out_dir.get())
        self.scenes.delete(0, "end")
        self.scene_items = []
        for n in self.scene_names():
            if flt and flt not in n.lower():
                continue
            done = (out / (n + ".c")).exists()
            self.scenes.insert("end", ("  " if done else "· ") + n)
            if not done:
                self.scenes.itemconfigure("end", foreground="#8a8a8a")
            self.scene_items.append(n)
        if keep in self.scene_items:
            i = self.scene_items.index(keep)
            self.scenes.selection_set(i)
            self.scenes.see(i)

    def selected_scene(self):
        sel = self.scenes.curselection()
        return self.scene_items[sel[0]] if sel else None

    def open_selected(self):
        name = self.selected_scene()
        if name:
            self.open_scene(name)

    def open_scene(self, name, line=None):
        path = Path(self.out_dir.get()) / (name + ".c")
        if not path.exists():
            self.set_text("// %s is not decompiled yet. Press 'Decompile selected'." % name, name)
            self.funcs, self.func_line = [], {}
            self.fill_funcs()
            return
        text = path.read_text(encoding="utf-8")
        self.current = name
        self.set_text(text, "%s.c — %d lines" % (name, text.count("\n")))
        self.funcs = []
        self.func_line = {}
        for m in FUNC_HEAD.finditer(text):
            ln = text.count("\n", 0, m.start(3)) + 1
            info = m.group(2).strip("; ")
            self.funcs.append((m.group(4), ln, info))
            self.func_line[m.group(4)] = ln
        self.fill_funcs()
        if line:
            self.goto_line(line)

    def fill_funcs(self):
        flt = self.func_filter.get().lower()
        self.funclist.delete(0, "end")
        self.func_items = []
        for name, ln, info in self.funcs:
            if flt and flt not in name and flt not in info.lower():
                continue
            tag = " ⟲" if "thread" in info else (" ★" if "entry" in info or "BASE" in info or "MAPEV" in info else "")
            self.funclist.insert("end", name + tag)
            self.func_items.append((name, ln))

    def goto_selected_func(self):
        sel = self.funclist.curselection()
        if sel:
            self.jump(self.func_items[sel[0]][1])

    # ---------------------------------------------------------------- code view
    def readonly_key(self, e):
        if e.state & 4 and e.keysym.lower() in ("c", "a", "f"):
            if e.keysym.lower() == "a":
                self.text.tag_add("sel", "1.0", "end")
                return "break"
            return None
        if e.keysym in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    def set_text(self, text, title):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.highlighted = set()
        self.title.configure(text=title)
        self.text.yview_moveto(0)
        self.root.after_idle(self.highlight_visible)

    def on_xscroll(self, *args):
        self.text.xview(*args)

    def highlight_visible(self):
        try:
            first = int(self.text.index("@0,0").split(".")[0])
            last = int(self.text.index("@0,%d" % self.text.winfo_height()).split(".")[0])
        except tk.TclError:
            return
        lo, hi = max(1, first - 60), last + 60
        chunk = (lo // 100, hi // 100)
        for c in range(chunk[0], chunk[1] + 1):
            if c in self.highlighted:
                continue
            self.highlighted.add(c)
            a, b = max(1, c * 100), c * 100 + 100
            seg = self.text.get("%d.0" % a, "%d.0" % b)
            for m in TOKEN.finditer(seg):
                kind = m.lastgroup
                s, e = "%d.0+%dc" % (a, m.start()), "%d.0+%dc" % (a, m.end())
                if kind == "call" and m.group(0) in self.func_line:
                    kind = "fnlink"
                self.text.tag_add(kind, s, e)
            for m in re.finditer(r"\bfn_[0-9a-f]+\b(?!\()", seg):
                if m.group(0) in self.func_line:
                    self.text.tag_add("fnlink", "%d.0+%dc" % (a, m.start()), "%d.0+%dc" % (a, m.end()))

    def goto_line(self, ln):
        self.text.tag_remove("current", "1.0", "end")
        self.text.tag_add("current", "%d.0" % ln, "%d.0 lineend+1c" % ln)
        self.text.yview("%d.0" % max(1, ln - 2))
        self.text.mark_set("insert", "%d.0" % ln)
        self.highlight_visible()

    def jump(self, ln):
        here = int(self.text.index("@0,0").split(".")[0]) + 2
        self.history.append((self.current, here))
        self.goto_line(ln)

    def back(self):
        if not self.history:
            return
        scene, ln = self.history.pop()
        if scene != self.current and scene:
            self.open_scene(scene, ln)
        else:
            self.goto_line(ln)

    def on_double(self, e):
        idx = self.text.index("@%d,%d" % (e.x, e.y))
        word = self.text.get(idx + " wordstart", idx + " wordend")
        if word in self.func_line:
            self.jump(self.func_line[word])
            return "break"
        return None

    def find_next(self):
        pat = self.search.get()
        if not pat:
            return
        self.text.tag_remove("hit", "1.0", "end")
        start = self.text.index("insert +1c")
        pos = self.text.search(pat, start, nocase=True, stopindex="end") or self.text.search(pat, "1.0", nocase=True, stopindex="end")
        if not pos:
            self.status.set("not found: %s" % pat)
            return
        end = "%s+%dc" % (pos, len(pat))
        self.text.tag_add("hit", pos, end)
        self.text.mark_set("insert", pos)
        self.text.see(pos)
        self.highlight_visible()
        self.status.set("found at line %s" % pos.split(".")[0])

    def edit(self):
        name = self.current or self.selected_scene()
        if not name:
            messagebox.showinfo("Edit", "Select a scene first.")
            return
        self.on_edit(name)

    def show_report(self):
        p = Path(self.out_dir.get()) / "report.txt"
        self.history.append((self.current, int(self.text.index("@0,0").split(".")[0])))
        self.current = None
        self.set_text(p.read_text(encoding="utf-8") if p.exists() else "No report yet. Run 'Decompile all'.", "report.txt")

    def show_header(self):
        p = Path(self.out_dir.get()) / "scn.h"
        self.history.append((self.current, int(self.text.index("@0,0").split(".")[0])))
        self.current = None
        self.set_text(p.read_text(encoding="utf-8") if p.exists() else "No scn.h yet. Run the decompiler first.", "scn.h")

    def open_code(self):
        out = Path(self.out_dir.get())
        target = out / (self.current + ".c") if self.current else out
        try:
            subprocess.Popen(["code", "-g", "%s:%s" % (target, self.text.index("@0,0").split(".")[0])] if self.current else ["code", str(out)], shell=True)
        except OSError as e:
            messagebox.showerror("VS Code", str(e))

    # ---------------------------------------------------------------- running the decompiler
    def run_all(self):
        self.start([sys.executable, "-u", str(HERE / "scn_decomp.py"), "all", self.scn_dir.get(), self.out_dir.get()], "decompiling all scenes")

    def run_selected(self):
        name = self.selected_scene()
        if not name:
            messagebox.showinfo("Decompile", "Select a scene first.")
            return
        scn = Path(self.scn_dir.get()) / (name + ".scn")
        if not scn.exists():
            messagebox.showerror("Decompile", "%s not found" % scn)
            return
        self.start([sys.executable, "-u", str(HERE / "scn_decomp.py"), "scene", str(scn), self.out_dir.get()], "decompiling " + name, reopen=name)

    def start(self, cmd, what, reopen=None):
        if self.proc:
            return
        self.save_settings()
        self.log.insert("end", "> %s\n" % " ".join(cmd))
        self.reopen = reopen
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.status.set(what + "…")
        self.progress.start(12)
        for b in (self.btn_all, self.btn_one):
            b.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line))
        self.q.put(("done", proc.wait()))

    def stop(self):
        if self.proc:
            self.proc.terminate()

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.log.insert("end", val)
                    self.log.see("end")
                else:
                    self.proc = None
                    self.progress.stop()
                    for b in (self.btn_all, self.btn_one):
                        b.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status.set("finished, exit code %d" % val)
                    self.log.insert("end", "[exit code %d]\n" % val)
                    self.log.see("end")
                    self.refresh_scenes()
                    if self.on_finished:
                        self.on_finished()
                    if self.reopen and val == 0:
                        self.open_scene(self.reopen)
                    elif val == 0 and not self.reopen:
                        self.show_report()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
