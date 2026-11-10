#!/usr/bin/env python3
"""SCN Studio: the scenario decompiler and compiler in one window.

  uv run --no-project --python-preference only-managed python scn_studio.py
  (or double-click scn_studio.cmd)

Decompiler tab: decompile .scn files to C and browse them.
Compiler tab:   edit scenes in the work/ folder, see changed functions, build .scn files.
"Edit in workspace →" on the Decompiler tab copies the open scene to the workspace and switches tabs.
"""
import tkinter as tk
from tkinter import ttk

import scn_compile_gui
import scn_gui


def main():
    root = tk.Tk()
    root.title("SCN Studio")
    root.geometry("1500x920")
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True)
    dec_tab, comp_tab = ttk.Frame(tabs), ttk.Frame(tabs)
    tabs.add(dec_tab, text="  Decompiler  ")
    tabs.add(comp_tab, text="  Compiler  ")
    apps = {}

    def edit(name):
        tabs.select(comp_tab)
        apps["comp"].edit_scene(name)

    def finished():
        apps["comp"].refresh()

    apps["dec"] = scn_gui.App(root, dec_tab, on_edit=edit, on_finished=finished, is_active=lambda: tabs.select() == str(dec_tab))
    apps["comp"] = scn_compile_gui.App(root, comp_tab, dec_var=apps["dec"].out_dir)
    apps["dec"].out_dir.trace_add("write", lambda *_: apps["comp"].refresh())
    root.mainloop()


if __name__ == "__main__":
    main()
