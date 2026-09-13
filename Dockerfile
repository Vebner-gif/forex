# Официальный образ Playwright уже содержит Chromium и все системные
# зависимости для него — без этого пришлось бы вручную ставить ~15 apt-пакетов
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forex_bot.py .

# subscribers.json, daily_state.json и passport_subscribers.json будут
# создаваться и жить здесь; на Render/Railway стоит подключить persistent
# volume на /app, иначе при каждом передеплое подписчики теряются.
CMD ["python", "forex_bot.py"]
