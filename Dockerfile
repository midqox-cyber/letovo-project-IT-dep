# GreenNet Crisis — образ приложения
FROM python:3.12-slim

WORKDIR /app

# Зависимости (кэшируется отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Код приложения
COPY app.py .
COPY static/ ./static/

# Данные (база + ключ сессий) хранятся в томе /data, чтобы переживать перезапуск
ENV GREENNET_DATA=/data \
    PORT=5000 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 5000

# Продакшн-сервер: 1 процесс + 16 потоков (безопасно для SQLite в WAL-режиме).
# Рассчитано на ~200 игроков с поллингом раз в 5с: почти все ответы — лёгкие 304 (ETag),
# поэтому потоков хватает с запасом. Один процесс = точный rate-limit и целостность SQLite.
CMD ["gunicorn", "-w", "1", "--threads", "16", "--timeout", "60", "-b", "0.0.0.0:5000", "app:app"]
