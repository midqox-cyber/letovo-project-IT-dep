@echo off
chcp 65001 >nul
title GreenNet Crisis
cd /d "%~dp0"

echo.
echo   ============================================
echo     GreenNet Crisis - запуск платформы
echo   ============================================
echo.

rem --- Способ 1: Docker (если установлен и запущен) ---
where docker >nul 2>nul
if not errorlevel 1 (
    echo   [Docker] Собираю образ и запускаю контейнер...
    echo   [Docker] Первый запуск может занять пару минут - идёт загрузка образа.
    echo.
    docker compose up --build -d
    rem 'if not errorlevel 1' читает код возврата ЗДЕСЬ И СЕЙЧАС (не при разборе блока)
    if not errorlevel 1 (
        echo.
        echo   Готово!  Открываю http://localhost:5000
        echo   Вход:  admin  (пароль сгенерирован - смотрите файл admin_password.txt рядом с базой)
        echo   Остановить:  запустите stop.bat
        timeout /t 4 >nul
        start "" http://localhost:5000
        echo   Окно можно закрыть - сервер работает в фоне.
        pause
        goto :eof
    )
    echo.
    echo   [Docker] Не удалось запустить через Docker (возможно, Docker Desktop не запущен).
    echo   [Docker] Пробую запуск через Python...
    echo.
)

rem --- Способ 2: Python (запасной вариант) ---
where python >nul 2>nul
if not errorlevel 1 (
    if not exist ".venv\Scripts\python.exe" (
        echo   [Python] Создаю локальное виртуальное окружение .venv...
        python -m venv .venv
    )
    if not exist ".venv\Scripts\python.exe" (
        echo   [Python] Не удалось создать виртуальное окружение.
        pause
        goto :eof
    )
    echo   [Python] Устанавливаю зависимости в локальное окружение...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    echo   [Python] Запускаю сервер. Откройте http://localhost:5000
    echo   Вход:  admin  (пароль сгенерирован - смотрите файл admin_password.txt рядом с базой)
    echo   База:  greennet.db  ^|  CSV пользователей: users.csv
    echo   Остановить сервер: закройте это окно или нажмите Ctrl+C.
    echo.
    rem открываем браузер с задержкой, чтобы сервер успел подняться
    start "" /min powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://localhost:5000'"
    ".venv\Scripts\python.exe" app.py
    goto :eof
)

rem --- Ничего не найдено ---
echo   Не найден ни Docker, ни Python.
echo   Установите одно из:
echo     - Docker Desktop:  https://www.docker.com/products/docker-desktop/
echo     - Python 3:        https://www.python.org/downloads/
echo.
pause
