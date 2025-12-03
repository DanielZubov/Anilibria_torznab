# Используем официальный Python slim образ
FROM python:3.12-slim

# Рабочая директория
WORKDIR /app

# Копируем только необходимые файлы
COPY requirements.txt .
COPY app.py .

# Установка зависимостей
RUN pip install --no-cache-dir -r requirements.txt

# Экспонируем порт
EXPOSE 8020

# Команда запуска uvicorn в продакшн режиме
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8020", "--workers", "4", "--timeout-keep-alive", "120"]
