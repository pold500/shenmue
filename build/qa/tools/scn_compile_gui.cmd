@echo off
rem Starts the SCN compiler GUI with a uv-managed Python (no system Python, no packages needed).
cd /d "%~dp0"
uv run --no-project --python-preference only-managed python scn_compile_gui.py
