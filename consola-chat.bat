@echo off
chcp 65001 >nul
title Consola Interactiva en Tiempo Real - Bridge
set REPO_DIR=C:\Users\LAZARINOS\codex-chatgpt-bridge-clean
if exist "%REPO_DIR%\.venv\Scripts\python.exe" (
    "%REPO_DIR%\.venv\Scripts\python.exe" "%REPO_DIR%\bridge_messenger.py" --chat %*
) else (
    python "%REPO_DIR%\bridge_messenger.py" --chat %*
)
