@echo off
rem Starts SCN Studio (decompiler + compiler) with a uv-managed Python (no system Python, no packages needed).
cd /d "%~dp0"
uv run --no-project --python-preference only-managed python scn_studio.py
