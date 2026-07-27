FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

EXPOSE 8080

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_bridge ./telegram_bridge

RUN useradd --create-home --uid 10001 bridge
USER bridge

# Access logging is disabled because Telegram Bot API paths contain the bot token.
CMD ["uvicorn", "telegram_bridge.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
