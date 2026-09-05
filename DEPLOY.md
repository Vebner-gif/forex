# Деплой бота, чтобы работал постоянно

## Шаг 0. Залить код в GitHub

```bash
git init
git add forex_bot.py requirements.txt Dockerfile .gitignore
git commit -m "forex bot"
git branch -M main
git remote add origin https://github.com/<твой_юзернейм>/<репозиторий>.git
git push -u origin main
```

## Вариант A: Render.com (рекомендую как самый простой)

1. Зайти на render.com, зарегистрироваться, привязать GitHub-аккаунт.
2. New → Background Worker (НЕ Web Service — боту не нужен веб-порт).
3. Выбрать репозиторий с ботом.
4. Runtime: Docker (подхватит Dockerfile автоматически).
5. В разделе Environment добавить переменные:
   - `BOT_TOKEN` = токен от @BotFather
   - `ANTHROPIC_API_KEY` = ключ с platform.claude.com
6. В разделе Disks добавить Persistent Disk, смонтировать на `/app` — иначе
   subscribers.json и daily_state.json будут стираться при каждом деплое, и
   подписка на бота слетит.
7. Deploy. Дальше любой `git push` в main будет автоматически передеплоивать
   бота.

## Вариант B: Railway.app

Тот же принцип: New Project → Deploy from GitHub repo → Railway сам находит
Dockerfile → добавляешь переменные окружения в Variables → подключаешь Volume
на `/app` для сохранения json-файлов между деплоями.

## Вариант C: свой VPS (Timeweb Cloud / Hetzner / DigitalOcean)

```bash
# на сервере
git clone https://github.com/<твой_юзернейм>/<репозиторий>.git
cd <репозиторий>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Создать systemd-юнит `/etc/systemd/system/forex-bot.service`:

```ini
[Unit]
Description=Forex Telegram Bot
After=network.target

[Service]
WorkingDirectory=/root/<репозиторий>
Environment="BOT_TOKEN=твой_токен"
Environment="ANTHROPIC_API_KEY=твой_ключ"
ExecStart=/root/<репозиторий>/venv/bin/python forex_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable forex-bot
sudo systemctl start forex-bot
sudo journalctl -u forex-bot -f   # смотреть логи
```

Обновление после `git push`:

```bash
cd <репозиторий>
git pull
sudo systemctl restart forex-bot
```

## Что важно в любом варианте

- Секреты (`BOT_TOKEN`, `ANTHROPIC_API_KEY`) — только через переменные
  окружения, никогда не коммитить в GitHub. `.gitignore` уже исключает
  `subscribers.json` и `daily_state.json`, где могут быть чужие chat_id.
- Без persistent-хранилища (Volume/Disk на Render/Railway, или просто файлы на
  VPS) подписчики и состояние дня будут теряться при каждом рестарте — учти
  это при выборе варианта.
