@echo off
REM ASCII-only on purpose. Chinese UI lives in menu.py.
REM cd to the script's own directory, so this bat works even if copied or moved.
cd /d "%~dp0"
REM Use chrome-use plugin driver (no debug port).
set BOSS_DRIVER=plugin
python menu.py
