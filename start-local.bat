@echo off
chcp 65001 >nul
title GreenNet Crisis - локальный сервер
cd /d "%~dp0"

echo.
echo   ============================================
echo     GreenNet Crisis - локальный сервер
echo   ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   Python не найден. Установите Python 3.9+:
    echo   https://www.python.org/downloads/
    echo.
    pause
    goto :eof
)

if not exist ".venv\Scripts\python.exe" (
    echo   [1/3] Создаю изолированное окружение .venv...
    python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo   Не удалось создать виртуальное окружение.
    pause
    goto :eof
)

echo   [2/3] Проверяю зависимости...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo   Не удалось установить Flask. Проверьте интернет-соединение.
    pause
    goto :eof
)

echo   [3/3] Запускаю http://localhost:5000
echo.
echo   Логин администратора: admin
echo   Пароль: admin_password.txt ^(создастся при первом запуске^)
echo   База: greennet.db ^| CSV: users.csv
echo   Для остановки закройте это окно или нажмите Ctrl+C.
echo.

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://localhost:5000'"
".venv\Scripts\python.exe" app.py
