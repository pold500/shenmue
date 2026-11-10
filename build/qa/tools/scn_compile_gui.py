#!/usr/bin/env python3
"""GUI for scn_compile.py: edit decompiled scenes in a workspace, see what changed, build .scn files.

  uv run --no-project --python-preference only-managed python scn_compile_gui.py
  (or double-click scn_compile_gui.cmd)

Workflow: pick a decompiled scene, "Add to workspace" copies it to work/, edit the .c (Open in VS Code),
"Build" writes out/scn_build/<scene>.scn. Double-click an error line to open it in VS Code.
"""
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk, filedialog

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "out" / "scn_compile_gui.json"
FUNC = re.compile(r"^/\* 0x([0-9a-f]+)[^\n]*\*/\n[^\n]*\b(fn_[0-9a-f]+)\([^\n]*\n(?:.*?\n)*?}", re.M)
ERROR_LINE = re.compile(r"^(.*?\.(?:c|h)):(\d+): error: ", re.M)
COLORS = {"bg": "#16191f", "fg": "#d8dee9", "add": "#8fcf9c", "del": "#ef8a80", "hunk": "#62bfd8", "err": "#ef8a80", "ok": "#8fcf9c", "dim": "#7f8a9a"}


def functions(text):
    return {m.group(2): m.group(0) for m in FUNC.finditer(text)}


class App:
    def __init__(self, root, parent=None, dec_var=None):
        """parent: frame to build into (a tab of scn_studio); dec_var: shared decompiled-folder variable."""
        self.root = root
        self.box = parent or root
        if parent is None:
            root.title("SCN compiler")
            root.geometry("1450x880")
        s = self.load_settings()
        self.src = tk.StringVar(value=s.get("src", str(HERE / "work")))
        self.dec = dec_var or tk.StringVar(value=s.get("dec", str(HERE / "out" / "scn_c")))
        self.scn = tk.StringVar(value=s.get("scn", str(HERE / "scn")))
        self.out = tk.StringVar(value=s.get("out", str(HERE / "out" / "scn_build")))
        self.filter = tk.StringVar()
        self.force = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="ready")
        self.proc = None
        self.q = queue.Queue()
        self.items = []
        self.after_done = None
        self.build_ui()
        self.refresh()
        root.after(100, self.poll)

    # ---------------------------------------------------------------- layout
    def build_ui(self):
        st = ttk.Style()
        if "vista" in st.theme_names():
            st.theme_use("vista")
        top = ttk.Frame(self.box, padding=6)
        top.pack(fill="x")
        rows = (("Workspace (.c)", self.src), ("Decompiled (.c)", self.dec), ("Original (.scn)", self.scn), ("Output (.scn)", self.out))
        for r, (label, var) in enumerate(rows):
            ttk.Label(top, text=label, width=15).grid(row=r, column=0, sticky="w")
            ttk.Entry(top, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=1)
            ttk.Button(top, text="Browse…", command=lambda v=var: self.pick(v)).grid(row=r, column=2, padx=2)
        top.columnconfigure(1, weight=1)
        bar = ttk.Frame(top)
        bar.grid(row=0, column=3, rowspan=4, padx=(12, 0), sticky="n")
        buttons = [
            ("Add to workspace", self.add_to_workspace), ("Check", self.check), ("Build", self.build),
            ("Build (rebuild)", self.rebuild), ("Build all changed", self.build_all), ("Verify", self.verify),
            ("Open in VS Code", self.open_code), ("Open output", self.open_out), ("Refresh", self.refresh),
        ]
        self.buttons = []
        for i, (text, cmd) in enumerate(buttons):
            b = ttk.Button(bar, text=text, command=cmd)
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
            self.buttons.append(b)
        ttk.Checkbutton(bar, text="Recompile every function", variable=self.force).grid(row=3, column=0, columnspan=3, sticky="w")

        main = ttk.PanedWindow(self.box, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6)
        left = ttk.Frame(main)
        main.add(left, weight=0)
        ttk.Label(left, text="Scenes").pack(anchor="w")
        ttk.Entry(left, textvariable=self.filter).pack(fill="x")
        self.filter.trace_add("write", lambda *_: self.refresh(keep_status=True))
        self.scenes = tk.Listbox(left, width=30, exportselection=False, activestyle="none", font=("Consolas", 10))
        self.scenes.pack(fill="both", expand=True)
        self.scenes.bind("<<ListboxSelect>>", lambda e: self.select_scene())
        ttk.Label(left, text="✎ n  changed functions   ·  unchanged\n○  not in workspace yet", foreground="#6b7787").pack(anchor="w")

        mid = ttk.Frame(main)
        main.add(mid, weight=0)
        ttk.Label(mid, text="Changed functions").pack(anchor="w")
        self.funcs = tk.Listbox(mid, width=22, exportselection=False, activestyle="none", font=("Consolas", 10))
        self.funcs.pack(fill="both", expand=True)
        self.funcs.bind("<<ListboxSelect>>", lambda e: self.show_func_diff())

        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=1)
        box = ttk.Frame(right)
        right.add(box, weight=4)
        self.title = ttk.Label(box, text="", font=("Segoe UI", 10, "bold"))
        self.title.pack(anchor="w")
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.view = tk.Text(frame, wrap="none", font=("Consolas", 11), bg=COLORS["bg"], fg=COLORS["fg"], insertbackground=COLORS["fg"])
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.view.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.view.xview)
        self.view.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.view.pack(fill="both", expand=True)
        for tag in ("add", "del", "hunk", "err", "ok", "dim"):
            self.view.tag_configure(tag, foreground=COLORS[tag])
        self.view.tag_configure("err", underline=True)
        self.view.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ("c", "a")) or e.keysym in ("Up", "Down", "Prior", "Next", "Home", "End", "Left", "Right") else "break")
        self.view.bind("<Double-Button-1>", self.on_double)
        logbox = ttk.Frame(right)
        right.add(logbox, weight=1)
        self.log = tk.Text(logbox, height=9, wrap="none", font=("Consolas", 9), bg="#0f1115", fg="#b8c2d0")
        ls = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground=COLORS["err"], underline=True)
        self.log.bind("<Double-Button-1>", self.on_double)
        sb = ttk.Frame(self.box, padding=(6, 2))
        sb.pack(fill="x")
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        ttk.Label(sb, textvariable=self.status).pack(side="left")
        self.show_text("Pick a scene. 'Add to workspace' copies a decompiled scene to the workspace folder so your edits are not "
                       "overwritten by the decompiler.\nEdit the .c, then 'Build'. The .scn goes to the output folder.\n\n"
                       "Build keeps every unchanged function byte for byte and compiles only the functions you edited.\n"
                       "'Build (rebuild)' lays out the whole file from C instead.\n"
                       "'Verify' rebuilds the selected scene, decompiles the result again and compares the C text.", "SCN compiler")

    # ---------------------------------------------------------------- settings
    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({"src": self.src.get(), "dec": self.dec.get(), "scn": self.scn.get(), "out": self.out.get()}))
        except OSError:
            pass

    def pick(self, var):
        d = filedialog.askdirectory(initialdir=var.get())
        if d:
            var.set(d)
            self.save_settings()
            self.refresh()

    # ---------------------------------------------------------------- scenes
    def refresh(self, keep_status=False):
        src, dec = Path(self.src.get()), Path(self.dec.get())
        keep = self.selected()
        names = sorted({p.stem for p in src.glob("*.c")} | {p.stem for p in dec.glob("*.c") if p.stem != "scn"})
        flt = self.filter.get().lower()
        self.scenes.delete(0, "end")
        self.items = []
        for n in names:
            if flt and flt not in n.lower():
                continue
            if (src / (n + ".c")).exists():
                changed = self.changed(n)
                label = "%s  %s" % (n, ("✎ %d" % len(changed)) if changed else "·") if changed is not None else "%s  ?" % n
            else:
                label = "○ " + n
            self.scenes.insert("end", label)
            if label.startswith("○"):
                self.scenes.itemconfigure("end", foreground="#8a8a8a")
            self.items.append(n)
        if keep in self.items:
            i = self.items.index(keep)
            self.scenes.selection_set(i)
            self.scenes.see(i)

    def changed(self, name):
        p = Path(self.src.get()) / (name + ".c")
        side = p.with_suffix(".json")
        if not side.exists():
            return None
        try:
            info = json.loads(side.read_text())
            hashes = {"fn_" + a: v["c_sha1"] for a, v in info["functions"].items()}
            import hashlib
            text = p.read_text(encoding="utf-8").replace("\r\n", "\n")
            return [n for n, body in functions(text).items() if hashes.get(n) != hashlib.sha1(body.encode("utf-8")).hexdigest()]
        except (OSError, ValueError, KeyError):
            return None

    def selected(self):
        sel = self.scenes.curselection()
        return self.items[sel[0]] if sel else None

    def select_scene(self):
        name = self.selected()
        self.funcs.delete(0, "end")
        if not name:
            return
        p = Path(self.src.get()) / (name + ".c")
        if not p.exists():
            self.show_text("%s is not in the workspace yet. Press 'Add to workspace' to copy it from the decompiled folder." % name, name)
            return
        changed = self.changed(name) or []
        for n in changed:
            self.funcs.insert("end", n)
        rep = Path(self.out.get()) / (name + ".build.json")
        lines = ["%s.c in the workspace: %d changed functions." % (name, len(changed))]
        if rep.exists():
            try:
                r = json.loads(rep.read_text())
                lines += ["", "Last build (%s mode): %s" % (r.get("mode"), r.get("output")), json.dumps(r, indent=1)]
            except ValueError:
                pass
        lines += ["", "Select a changed function to see its diff against the decompiled original."]
        self.show_text("\n".join(lines), name)

    def show_func_diff(self):
        name = self.selected()
        sel = self.funcs.curselection()
        if not name or not sel:
            return
        fn = self.funcs.get(sel[0])
        work = functions((Path(self.src.get()) / (name + ".c")).read_text(encoding="utf-8").replace("\r\n", "\n"))
        orig_path = Path(self.dec.get()) / (name + ".c")
        orig = functions(orig_path.read_text(encoding="utf-8")) if orig_path.exists() else {}
        a = orig.get(fn, "").split("\n")
        b = work.get(fn, "").split("\n")
        diff = list(difflib.unified_diff(a, b, "decompiled", "workspace", lineterm="", n=4))
        self.view.delete("1.0", "end")
        self.title.configure(text="%s — %s" % (name, fn))
        if not orig.get(fn):
            self.view.insert("end", "New function (not in the decompiled scene):\n\n", "dim")
            self.view.insert("end", work.get(fn, ""))
            return
        for line in diff or ["(no textual difference)"]:
            tag = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else "hunk" if line.startswith("@@") else None
            self.view.insert("end", line + "\n", tag)

    def show_text(self, text, title):
        self.view.delete("1.0", "end")
        self.title.configure(text=title)
        for line in text.split("\n"):
            tag = "err" if ERROR_LINE.match(line) else ("ok" if line.endswith(": ok") or "identical" in line else None)
            self.view.insert("end", line + "\n", tag)

    def on_double(self, e):
        w = e.widget
        line = w.get("@%d,%d linestart" % (e.x, e.y), "@%d,%d lineend" % (e.x, e.y))
        m = ERROR_LINE.match(line.strip())
        if m:
            self.code_at(m.group(1), m.group(2))
            return "break"
        return None

    def code_at(self, path, line):
        try:
            subprocess.Popen(["code", "-g", "%s:%s" % (path, line)], shell=True)
        except OSError as ex:
            messagebox.showerror("VS Code", str(ex))

    # ---------------------------------------------------------------- actions
    def add_to_workspace(self):
        name = self.selected()
        if not name:
            return messagebox.showinfo("Workspace", "Select a scene first.")
        src, dec = Path(self.src.get()), Path(self.dec.get())
        target = src / (name + ".c")
        if target.exists() and not messagebox.askyesno("Workspace", "%s is already in the workspace. Replace it with the decompiled version? Your edits will be lost." % target.name):
            return
        if not (dec / (name + ".c")).exists():
            return messagebox.showerror("Workspace", "%s.c not found in %s" % (name, dec))
        src.mkdir(parents=True, exist_ok=True)
        for ext in (".c", ".json"):
            shutil.copy2(dec / (name + ext), src / (name + ext))
        if (dec / "scn.h").exists():
            shutil.copy2(dec / "scn.h", src / "scn.h")
        self.log.insert("end", "copied %s.c and %s.json to %s\n" % (name, name, src))
        self.refresh()
        self.select_scene()

    def edit_scene(self, name):
        """select a scene; copy it from the decompiled folder first if it is not in the workspace yet"""
        self.filter.set("")
        self.refresh()
        if name not in self.items:
            messagebox.showerror("Workspace", "%s.c is not in the decompiled folder %s" % (name, self.dec.get()))
            return
        i = self.items.index(name)
        self.scenes.selection_clear(0, "end")
        self.scenes.selection_set(i)
        self.scenes.see(i)
        if not (Path(self.src.get()) / (name + ".c")).exists():
            self.add_to_workspace()
        else:
            self.select_scene()

    def need_scene(self):
        name = self.selected()
        p = Path(self.src.get()) / ((name or "") + ".c")
        if not name or not p.exists():
            messagebox.showinfo("SCN compiler", "Select a scene that is in the workspace.")
            return None
        return p

    def compiler(self, *args):
        return [sys.executable, "-u", str(HERE / "scn_compile.py")] + list(args)

    def check(self):
        p = self.need_scene()
        if p:
            self.run(self.compiler("check", str(p)), "checking " + p.stem)

    def build(self, rebuild=False):
        p = self.need_scene()
        if not p:
            return
        args = ["build", str(p), "--scn", self.scn.get(), "--out", self.out.get()]
        if rebuild:
            args.append("--rebuild")
        elif self.force.get():
            args.append("--force")
        self.run(self.compiler(*args), "building " + p.stem)

    def rebuild(self):
        self.build(rebuild=True)

    def build_all(self):
        src = Path(self.src.get())
        todo = [n for n in sorted(p.stem for p in src.glob("*.c")) if self.changed(n)]
        if not todo:
            return messagebox.showinfo("SCN compiler", "No scene in the workspace has changed functions.")
        script = "; ".join('& "%s" -u "%s" build "%s" --scn "%s" --out "%s"' % (sys.executable, HERE / "scn_compile.py", src / (n + ".c"), self.scn.get(), self.out.get()) for n in todo)
        self.run(["powershell", "-NoProfile", "-Command", script], "building %d scenes" % len(todo))

    def verify(self):
        p = self.need_scene()
        if p:
            self.after_done = lambda: self.show_file(HERE / "out" / "verify" / "verify.txt")
            self.run(self.compiler("verify", "--src", self.src.get(), "--scn", self.scn.get(), p.stem), "verifying " + p.stem)

    def show_file(self, path):
        if Path(path).exists():
            self.show_text(Path(path).read_text(encoding="utf-8"), Path(path).name)

    def open_code(self):
        p = self.need_scene()
        if p:
            subprocess.Popen(["code", str(p)], shell=True)

    def open_out(self):
        out = Path(self.out.get())
        out.mkdir(parents=True, exist_ok=True)
        os.startfile(str(out))

    # ---------------------------------------------------------------- process
    def run(self, cmd, what):
        if self.proc:
            return
        self.save_settings()
        self.log.insert("end", "> %s\n" % " ".join(str(c) for c in cmd))
        self.collected = []
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.status.set(what + "…")
        self.progress.start(12)
        for b in self.buttons:
            b.configure(state="disabled")

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line))
        self.q.put(("done", proc.wait()))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.collected.append(val.rstrip("\n"))
                    self.log.insert("end", val, "err" if ERROR_LINE.match(val) else None)
                    self.log.see("end")
                else:
                    self.proc = None
                    self.progress.stop()
                    for b in self.buttons:
                        b.configure(state="normal")
                    self.status.set("finished, exit code %d" % val)
                    self.log.insert("end", "[exit code %d]\n" % val)
                    self.log.see("end")
                    text = "\n".join(self.collected)
                    if self.after_done:
                        cb, self.after_done = self.after_done, None
                        cb()
                    else:
                        self.show_text(text or "(no output)", "result")
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
