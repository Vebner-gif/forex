"""
Telegram-бот: форекс-сводка + прогнозы перед важными релизами.

Логика (проверяется раз в 5 минут, но реально что-то делает только по расписанию):
1. Каждый день бот смотрит календарь (ForexFactory) на сегодня.
2. Если сегодня ЕСТЬ важные (High impact) релизы по основным валютам:
   - за 1 час до КАЖДОГО такого релиза бот присылает прогноз-предположение:
     как обычно эта статистика влияет на валюту, чего ждёт рынок (по
     прогнозу/предыдущему значению) и что будет, если факт выйдет выше/ниже
     ожиданий — с учётом свежих новостных заголовков за последние часы.
3. Если сегодня важных релизов НЕТ:
   - один раз, перед открытием NYSE (9:30 по Нью-Йорку), присылает общую
     сводку факторов, которые могут повлиять на рынок сегодня, на основе
     последних новостных заголовков.
4. /start подписывает на уведомления и сразу присылает сводку календаря на
   сегодня. Команда /forecast — по запросу короткий прогноз-настроение
   (бычье/медвежье/нейтральное) по каждой из основных валют, золоту и нефти.

ВАЖНО: это фоновый процесс, должен работать круглосуточно на чём-то always-on
(VPS/сервер), не на ноутбуке, который выключается.

Нужные вводные:
1. BOT_TOKEN         — токен от @BotFather (обязателен).
2. ANTHROPIC_API_KEY — ключ с platform.claude.com (опционален). Без него бот
   всё равно работает: календарь по /start и сырые цифры по расписанию
   отправляются как обычно, просто вместо AI-анализа (прогноз-предположение,
   сводка перед NYSE, /forecast) будет пометка, что AI недоступен, и сырые
   данные без интерпретации. Как только ключ появится — просто добавь
   переменную окружения и перезапусти бота, код менять не нужно.

Зависимости (requirements.txt):
    aiogram, aiohttp, feedparser, anthropic

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="..."
    export ANTHROPIC_API_KEY="..."
    python forex_bot.py
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from anthropic import AsyncAnthropic

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_ENABLED = bool(ANTHROPIC_API_KEY)
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RSS_FEEDS = [
    "https://www.forexlive.com/feed/news",
    "https://www.fxstreet.com/rss/news",
    "https://www.investing.com/rss/news_25.rss",
    "https://oilprice.com/rss/main",
    "https://www.kitco.com/rss/KitcoNews.xml",
]
CRYPTO_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
COINGECKO_CHART_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"

MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪️"}

NY_TZ = ZoneInfo("America/New_York")
NYSE_OPEN = time(9, 30)
PRE_EVENT_LEAD = timedelta(hours=1)   # прогноз за час до важного релиза
PRE_NYSE_LEAD = timedelta(minutes=30)  # сводка за 30 мин до открытия NYSE
POLL_INTERVAL = 5 * 60  # как часто "просыпаться" и сверяться с расписанием

DATA_DIR = Path(__file__).parent
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
STATE_FILE = DATA_DIR / "daily_state.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if CLAUDE_ENABLED else None


# ---------- Хранилище ----------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_subscribers() -> set[int]:
    return set(load_json(SUBSCRIBERS_FILE, []))


def add_subscriber(chat_id: int) -> None:
    subs = get_subscribers()
    if chat_id not in subs:
        subs.add(chat_id)
        save_json(SUBSCRIBERS_FILE, list(subs))


def load_state() -> dict:
    state = load_json(STATE_FILE, {})
    today_str = date.today().isoformat()
    if state.get("date") != today_str:
        state = {"date": today_str, "notified_events": [], "quiet_summary_sent": False}
        save_json(STATE_FILE, state)
    return state


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


# ---------- Экономический календарь ----------

def parse_event_time(raw: str):
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


async def fetch_calendar() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(CALENDAR_URL, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json()


def filter_today(events: list[dict], impacts: tuple[str, ...]) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    result = []
    for ev in events:
        dt = parse_event_time(ev.get("date", ""))
        if dt is None or dt.astimezone(timezone.utc).date() != today:
            continue
        if ev.get("country") not in MAJOR_CURRENCIES:
            continue
        if ev.get("impact") not in impacts:
            continue
        ev = {**ev, "_dt": dt, "_id": f"{ev.get('country')}_{ev.get('title')}_{ev.get('date')}"}
        result.append(ev)
    result.sort(key=lambda e: e["_dt"])
    return result


def format_calendar_summary(events: list[dict]) -> str:
    if not events:
        return "📅 Сегодня нет значимых экономических событий по основным валютам."
    lines = ["📊 <b>Экономическая сводка на сегодня</b>\n"]
    for ev in events:
        emoji = IMPACT_EMOJI.get(ev.get("impact"), "⚪️")
        time_str = ev["_dt"].astimezone().strftime("%H:%M")
        lines.append(
            f"{emoji} <b>{time_str} {ev.get('country')}</b> — {ev.get('title')}\n"
            f"    Прогноз: {ev.get('forecast') or '—'} | Пред.: {ev.get('previous') or '—'}"
        )
    lines.append("\n<i>Источник: ForexFactory calendar</i>")
    return "\n".join(lines)


# ---------- Новости (контекст для прогнозов) ----------

def fetch_recent_headlines(limit: int = 12) -> list[str]:
    return _fetch_rss_headlines(RSS_FEEDS, limit)


def fetch_crypto_headlines(limit: int = 10) -> list[str]:
    return _fetch_rss_headlines(CRYPTO_RSS_FEEDS, limit)


def _fetch_rss_headlines(feeds: list[str], limit: int) -> list[str]:
    headlines = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            headlines.extend(e.get("title", "") for e in feed.entries[:8])
        except Exception:
            logger.exception(f"Не удалось прочитать RSS: {url}")
    return headlines[:limit]


def translate_to_ru(texts: list[str]) -> list[str]:
    """Бесплатный перевод заголовков на русский для fallback-режима (без
    Claude). Используется только когда ANTHROPIC_API_KEY не настроен —
    Claude сам прекрасно читает английские заголовки и в переводе не
    нуждается."""
    if not texts:
        return texts
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="ru").translate_batch(texts)
    except Exception:
        logger.exception("Не удалось перевести заголовки, оставляю оригинал")
        return texts


# ---------- Данные по биткоину (реальные цены, не выдумка) ----------

async def fetch_btc_market_data() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(COINGECKO_PRICE_URL, timeout=15) as resp:
            resp.raise_for_status()
            price_data = await resp.json()
        async with session.get(COINGECKO_CHART_URL, timeout=15) as resp:
            resp.raise_for_status()
            chart_data = await resp.json()

    current_price = price_data["bitcoin"]["usd"]
    change_24h = price_data["bitcoin"].get("usd_24h_change", 0.0)
    prices = [p[1] for p in chart_data.get("prices", [])]
    prices_7d = prices[-7:] if len(prices) >= 7 else prices

    return {
        "price": current_price,
        "change_24h": change_24h,
        "low_7d": min(prices_7d) if prices_7d else current_price,
        "high_7d": max(prices_7d) if prices_7d else current_price,
        "low_30d": min(prices) if prices else current_price,
        "high_30d": max(prices) if prices else current_price,
    }


def format_btc_raw(data: dict) -> str:
    return (
        f"Текущая цена: ${data['price']:,.0f} ({data['change_24h']:+.2f}% за 24ч)\n"
        f"Диапазон за 7 дней: ${data['low_7d']:,.0f} – ${data['high_7d']:,.0f}\n"
        f"Диапазон за 30 дней: ${data['low_30d']:,.0f} – ${data['high_30d']:,.0f}"
    )


# ---------- Генерация прогнозов через Claude ----------

PREDICT_PROMPT = """Ты аналитик форекс-рынка. Через час выходит статистика:

Страна/валюта: {currency}
Показатель: {title}
Прогноз рынка: {forecast}
Предыдущее значение: {previous}

Свежие новостные заголовки последних часов (могут быть не по теме — учитывай
только релевантные):
{headlines}

Напиши короткий прогноз по-русски (4-5 предложений):
- как этот показатель обычно влияет на валюту;
- если факт выйдет ЛУЧШЕ прогноза — что вероятно произойдёт с валютой;
- если факт выйдет ХУЖЕ прогноза — что вероятно произойдёт с валютой;
- отдельно — как это может отразиться на золоте (XAU/USD) и нефти (WTI/Brent),
  если показатель по USD (доллар и золото/нефть обычно двигаются в противофазе,
  доллар и нефть — по-разному в зависимости от причины движения);
- если в заголовках есть релевантный контекст (в т.ч. по золоту/нефти/OPEC), учти его.
Без общих фраз и дисклеймеров, только суть."""

QUIET_PROMPT = """Ты аналитик форекс и товарных рынков. Сегодня нет запланированных
важных экономических релизов по основным валютам. Перед открытием NYSE дай
короткую сводку по-русски (4-6 предложений) на основе свежих новостных
заголовков: что может повлиять сегодня на доллар и другие основные валюты, а
также отдельно — на золото (XAU/USD) и нефть (WTI/Brent), если такие факторы
вообще просматриваются в заголовках (геополитика, решения OPEC+, запасы нефти,
спрос на защитные активы и т.п.). Если ничего значимого нет — так и скажи
одной фразой, не выдумывай.

Заголовки:
{headlines}"""

FORECAST_PROMPT = """Ты аналитик форекс и товарных рынков. Вот экономические
события на сегодня (High/Medium impact) по основным валютам:
{calendar_summary}

Свежие новостные заголовки:
{headlines}

Дай короткое настроение (бычье/медвежье/нейтральное) по каждой из позиций:
USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD, Золото (XAU), Нефть (WTI/Brent).

Формат — строго по одной строке на каждую позицию, на русском:
<эмодзи 📈 или 📉 или ➡️> <Валюта/актив>: <причина в 5-10 слов>

Если по позиции нет значимых факторов сегодня — напиши "нет выраженного драйвера".
Без вступления, без заключения, без дисклеймеров — только список из 10 строк."""

BTC_PROMPT = """Ты крипто-аналитик. Вот реальные рыночные данные по биткоину:

{raw_data}

Свежие крипто-новостные заголовки:
{headlines}

Напиши короткий спекулятивный анализ по-русски (5-7 предложений):
- назови 2-3 конкретные зоны поддержки (в USD) на основе диапазонов выше и
  ближайших круглых психологических уровней;
- назови 2-3 конкретные зоны сопротивления (в USD) по той же логике;
- короткое предположение о вероятном направлении на ближайшие дни с учётом
  динамики 24ч/7д/30д и релевантных заголовков (если такие есть);
- обязательно заверши фразой, что это не финансовый совет и рынок крайне
  волатилен.
Никаких общих фраз — только конкретные уровни и суть."""


NO_KEY_NOTICE = "🤖 <i>AI-анализ пока недоступен (ANTHROPIC_API_KEY не настроен/не оплачен) — ниже сырые данные без интерпретации.</i>\n\n"


async def ask_claude(prompt: str, fallback: str) -> str:
    """Возвращает ответ Claude, если ключ настроен, иначе — заглушку fallback
    с пометкой, что AI-анализ временно недоступен."""
    if not CLAUDE_ENABLED:
        return NO_KEY_NOTICE + fallback
    try:
        resp = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        logger.exception("Ошибка запроса к Claude, отдаю сырые данные")
        return NO_KEY_NOTICE + fallback


async def send_to_subscribers(text: str) -> None:
    for chat_id in get_subscribers():
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception(f"Не удалось отправить сообщение в {chat_id}")


# ---------- Хендлеры бота ----------

@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    add_subscriber(message.chat.id)
    await message.answer("Подписал на уведомления. Собираю сводку на сегодня...")
    try:
        raw_events = await fetch_calendar()
    except Exception as e:
        await message.answer(f"Не удалось получить календарь: {e}")
        return
    events = filter_today(raw_events, ("High", "Medium"))
    await message.answer(format_calendar_summary(events), parse_mode="HTML")


@dp.message(Command("forecast"))
async def on_forecast(message: Message) -> None:
    await message.answer("Строю прогноз по валютам, золоту и нефти...")
    try:
        raw_events = await fetch_calendar()
    except Exception as e:
        await message.answer(f"Не удалось получить календарь: {e}")
        return
    events = filter_today(raw_events, ("High", "Medium"))
    headlines = fetch_recent_headlines()
    prompt = FORECAST_PROMPT.format(
        calendar_summary=format_calendar_summary(events),
        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
    )
    fallback = format_calendar_summary(events)
    forecast = await ask_claude(prompt, fallback)
    await message.answer(f"🔮 <b>Быстрый прогноз по валютам</b>\n\n{forecast}", parse_mode="HTML")


@dp.message(Command("btc"))
async def on_btc(message: Message) -> None:
    await message.answer("Собираю данные по биткоину...")
    try:
        data = await fetch_btc_market_data()
    except Exception as e:
        await message.answer(f"Не удалось получить данные по BTC: {e}")
        return
    headlines = fetch_crypto_headlines()
    raw_data = format_btc_raw(data)
    prompt = BTC_PROMPT.format(
        raw_data=raw_data,
        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
    )
    analysis = await ask_claude(prompt, raw_data)
    text = f"₿ <b>Биткоин: ${data['price']:,.0f} ({data['change_24h']:+.2f}% 24ч)</b>\n\n{analysis}"
    await message.answer(text, parse_mode="HTML")


# ---------- Планировщик ----------

async def scheduler_loop() -> None:
    while True:
        try:
            state = load_state()
            raw_events = await fetch_calendar()
            high_events = filter_today(raw_events, ("High",))
            now_utc = datetime.now(timezone.utc)

            if high_events:
                for ev in high_events:
                    if ev["_id"] in state["notified_events"]:
                        continue
                    trigger_at = ev["_dt"].astimezone(timezone.utc) - PRE_EVENT_LEAD
                    if now_utc >= trigger_at:
                        headlines = fetch_recent_headlines()
                        raw_fallback = (
                            f"Прогноз рынка: {ev.get('forecast') or 'нет данных'}\n"
                            f"Предыдущее значение: {ev.get('previous') or 'нет данных'}"
                        )
                        prediction = await ask_claude(PREDICT_PROMPT.format(
                            currency=ev.get("country"),
                            title=ev.get("title"),
                            forecast=ev.get("forecast") or "нет данных",
                            previous=ev.get("previous") or "нет данных",
                            headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
                        ), raw_fallback)
                        time_str = ev["_dt"].astimezone().strftime("%H:%M")
                        text = (
                            f"🔮 <b>Через час: {ev.get('country')} — {ev.get('title')} ({time_str})</b>\n\n"
                            f"{prediction}"
                        )
                        await send_to_subscribers(text)
                        state["notified_events"].append(ev["_id"])
                        save_state(state)
            else:
                ny_now = datetime.now(NY_TZ)
                trigger_at_ny = datetime.combine(ny_now.date(), NYSE_OPEN, tzinfo=NY_TZ) - PRE_NYSE_LEAD
                if ny_now >= trigger_at_ny and not state["quiet_summary_sent"]:
                    headlines = fetch_recent_headlines()
                    fallback_headlines = headlines if CLAUDE_ENABLED else translate_to_ru(headlines)
                    fallback = "Важных релизов сегодня нет. Свежие заголовки:\n" + (
                        "\n".join(f"- {h}" for h in fallback_headlines) or "нет свежих заголовков"
                    )
                    summary = await ask_claude(QUIET_PROMPT.format(
                        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
                    ), fallback)
                    text = "🗞 <b>Сводка перед открытием NYSE</b>\n\n" + summary
                    await send_to_subscribers(text)
                    state["quiet_summary_sent"] = True
                    save_state(state)

            logger.info("Цикл планировщика завершён")
        except Exception:
            logger.exception("Ошибка в планировщике")

        await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")
    if not CLAUDE_ENABLED:
        logger.warning("ANTHROPIC_API_KEY не задан — бот работает без AI-анализа, только сырые данные")
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
