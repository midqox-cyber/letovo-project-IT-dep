@echo off
chcp 65001 >nul
title GreenNet Crisis - остановка
cd /d "%~dp0"

where docker >nul 2>nul
if not errorlevel 1 (
    echo   Останавливаю контейнер GreenNet Crisis...
    docker compose down
    echo   Готово. Данные сохранены в Docker-томе.
) else (
    echo   Docker не найден. Если сервер запущен через Python -
    echo   просто закройте окно с ним или нажмите там Ctrl+C.
)
echo.
pause
