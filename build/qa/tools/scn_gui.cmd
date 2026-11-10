@echo off
rem Starts the SCN decompiler GUI with a uv-managed Python (no system Python, no packages needed).
cd /d "%~dp0"
uv run --no-project --python-preference only-managed python scn_gui.py
