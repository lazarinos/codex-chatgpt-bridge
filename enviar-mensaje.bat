@echo off
title Bridge - Mensajeria en Vivo
set REPO_DIR=C:\Users\LAZARINOS\codex-chatgpt-bridge-clean
if exist "%REPO_DIR%\.venv\Scripts\python.exe" (
    "%REPO_DIR%\.venv\Scripts\python.exe" "%REPO_DIR%\bridge_messenger.py" %*
) else (
    python "%REPO_DIR%\bridge_messenger.py" %*
)
