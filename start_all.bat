@echo off
chcp 65001 >nul
REM ============================================================
REM  一键开启知识库「前端 + 后端」
REM   - 前端/REST 后端: app.py        -> http://localhost:8000
REM   - MCP 工具后端:    python -m kb_mcp_server (stdio 常驻)
REM  用法:
REM   直接双击本文件           启动前后端
REM   双击后在窗口按 Ctrl+C     停止
REM   或: start_all.bat --stop  停止已拉起的进程
REM ============================================================
setlocal

REM 确定 python 解释器: 优先 PATH, 回退到本机 venv
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
    set "PY=C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)

if not exist "%PY%" (
    echo [错误] 找不到 python, 请先安装 Python 3.13 或确认 venv 路径。
    pause
    exit /b 1
)

cd /d "%~dp0"
echo [启动] 使用解释器: %PY%
"%PY%" start_all.py %*
endlocal
