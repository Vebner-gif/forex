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
   (бычье/медвежье/нейтральное) по каждой из основных валют, золоту, нефти
   и индексам (DAX 40, Nasdaq, S&P 500). Команда /btc — отдельный разбор
   биткоина с зонами поддержки/сопротивления. Команда /pairs — кнопки с
   популярными парами (EUR/USD, GBP/USD и т.д.), по нажатию — короткий
   анализ по этой паре с реальными ценовыми уровнями (ЕЦБ-курсы для фиатных
   пар, Binance для BTC) и позиционированием крупных трейдеров (COT-отчёты
   CFTC). Команда /pair EURUSD — то же самое текстом. Команда /indices —
   кнопки DAX 40 / Nasdaq / S&P 500, /index DAX40 — то же текстом. Команда
   /news — дайджест из двух блоков: «Главное» (что реально произошло, по
   фактам) и «Что это может значить» (короткий вывод).

Источники данных:
- Экономический календарь: ForexFactory (нюфид, кэш 15 мин).
- Новости: ForexLive, FXStreet, Investing.com, DailyFX, TradingEconomics,
  Oilprice, Kitco, MarketWatch, CNBC + официальные пресс-релизы ФРС, ЕЦБ,
  Банка Англии.
- Курсы фиатных пар: Frankfurter.app (данные ЕЦБ, без ключа).
- Цена BTC: Binance (без ключа).
- Индексы (DAX 40, Nasdaq, S&P 500): Yahoo Finance chart API (без ключа).
- Позиционирование трейдеров: CFTC Commitment of Traders (публичные данные,
  обновляются раз в неделю, по пятницам).

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
3. CLAUDE_MODEL      — необязательно, по умолчанию "claude-haiku-4-5-20251001"
   (дешёвая модель). Чтобы попробовать более сильную — задай в переменных
   окружения, например "claude-sonnet-4-6". Учти: Sonnet примерно в 3 раза
   дороже за токен, чем Haiku.

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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from anthropic import AsyncAnthropic

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_ENABLED = bool(ANTHROPIC_API_KEY)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RSS_FEEDS = [
    "https://www.forexlive.com/feed/news",
    "https://www.fxstreet.com/rss/news",
    "https://www.investing.com/rss/news_25.rss",
    "https://oilprice.com/rss/main",
    "https://www.kitco.com/rss/KitcoNews.xml",
    "https://www.dailyfx.com/feeds/all",
    "https://tradingeconomics.com/rss/news.aspx",
    # официальные пресс-релизы центробанков — первичный источник, не пересказ
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.ecb.europa.eu/rss/press.xml",
    "https://www.bankofengland.co.uk/rss/news",
]
CRYPTO_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
EQUITY_RSS_FEEDS = [
    "https://www.marketwatch.com/rss/topstories",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
COINGECKO_CHART_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30"

# Фондовые индексы через Yahoo Finance chart API (бесплатно, без ключа)
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
INDEX_ASSETS = {
    "DAX40": {"symbol": "^GDAXI", "label": "DAX 40"},
    "NASDAQ": {"symbol": "^IXIC", "label": "Nasdaq Composite"},
    "SP500": {"symbol": "^GSPC", "label": "S&P 500"},
}

# Курсы фиатных валют (ЕЦБ через Frankfurter.app — бесплатно, без ключа)
FRANKFURTER_URL = "https://api.frankfurter.app/{start}..{end}"

# COT-отчёты CFTC (позиционирование крупных спекулянтов по фьючерсам, раз в неделю)
COT_DATASET_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
COT_CONTRACT_NAMES = {
    "EUR": "EURO FX",
    "GBP": "BRITISH POUND STERLING",
    "JPY": "JAPANESE YEN",
    "CHF": "SWISS FRANC",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "NZD": "NEW ZEALAND DOLLAR",
}
COT_CACHE_TTL = timedelta(days=1)

CALENDAR_CACHE_TTL = timedelta(minutes=15)  # чтобы не ловить 429 от ForexFactory

MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪️"}

# Популярные пары для /pairs (кнопки) и /pair (текстом)
POPULAR_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "XAUUSD", "BTCUSD"]

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

_calendar_cache: dict = {"data": None, "fetched_at": None}
_cot_cache: dict = {}  # currency -> (fetched_at, data)

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
    """Кэшируем на CALENDAR_CACHE_TTL, чтобы частые /start и планировщик не
    ловили 429 Too Many Requests от ForexFactory."""
    now = datetime.now(timezone.utc)
    if _calendar_cache["data"] is not None and now - _calendar_cache["fetched_at"] < CALENDAR_CACHE_TTL:
        return _calendar_cache["data"]
    async with aiohttp.ClientSession() as session:
        async with session.get(CALENDAR_URL, timeout=15) as resp:
            resp.raise_for_status()
            data = await resp.json()
    _calendar_cache["data"] = data
    _calendar_cache["fetched_at"] = now
    return data


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


def fetch_equity_headlines(limit: int = 10) -> list[str]:
    return _fetch_rss_headlines(EQUITY_RSS_FEEDS, limit)


def fetch_all_headlines(limit: int = 20) -> list[str]:
    """Объединённый пул для дайджеста новостей: форекс/макро + акции + крипто."""
    combined = RSS_FEEDS + EQUITY_RSS_FEEDS + CRYPTO_RSS_FEEDS
    return _fetch_rss_headlines(combined, limit)


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
    """Binance вместо CoinGecko: не требует ключа и заметно реже блокирует
    облачные IP (Railway/Render и т.п.)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_TICKER_URL, timeout=15) as resp:
            resp.raise_for_status()
            ticker = await resp.json()
        async with session.get(BINANCE_KLINES_URL, timeout=15) as resp:
            resp.raise_for_status()
            klines = await resp.json()

    current_price = float(ticker["lastPrice"])
    change_24h = float(ticker["priceChangePercent"])

    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    klines_7d = klines[-7:] if len(klines) >= 7 else klines
    highs_7d = [float(k[2]) for k in klines_7d]
    lows_7d = [float(k[3]) for k in klines_7d]

    return {
        "price": current_price,
        "change_24h": change_24h,
        "low_7d": min(lows_7d) if lows_7d else current_price,
        "high_7d": max(highs_7d) if highs_7d else current_price,
        "low_30d": min(lows) if lows else current_price,
        "high_30d": max(highs) if highs else current_price,
    }


def format_btc_raw(data: dict) -> str:
    return (
        f"Текущая цена: ${data['price']:,.0f} ({data['change_24h']:+.2f}% за 24ч)\n"
        f"Диапазон за 7 дней: ${data['low_7d']:,.0f} – ${data['high_7d']:,.0f}\n"
        f"Диапазон за 30 дней: ${data['low_30d']:,.0f} – ${data['high_30d']:,.0f}"
    )


# ---------- Фондовые индексы (Yahoo Finance, бесплатно, без ключа) ----------

async def fetch_index_data(symbol: str) -> dict:
    url = YAHOO_CHART_URL.format(symbol=symbol)
    headers = {"User-Agent": "Mozilla/5.0"}  # Yahoo иногда блокирует запросы без UA
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            data = await resp.json()

    result = data["chart"]["result"][0]
    meta = result["meta"]
    closes = result["indicators"]["quote"][0]["close"]
    closes = [c for c in closes if c is not None]

    current = meta.get("regularMarketPrice", closes[-1] if closes else 0.0)
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or current
    change_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0.0
    closes_7d = closes[-7:] if len(closes) >= 7 else closes

    return {
        "price": current,
        "change_pct": change_pct,
        "low_7d": min(closes_7d) if closes_7d else current,
        "high_7d": max(closes_7d) if closes_7d else current,
        "low_30d": min(closes) if closes else current,
        "high_30d": max(closes) if closes else current,
    }


def format_index_raw(label: str, data: dict) -> str:
    return (
        f"{label}: {data['price']:,.0f} ({data['change_pct']:+.2f}% за посл. сессию)\n"
        f"Диапазон за 7 дней: {data['low_7d']:,.0f} – {data['high_7d']:,.0f}\n"
        f"Диапазон за 30 дней: {data['low_30d']:,.0f} – {data['high_30d']:,.0f}"
    )


# ---------- Реальные курсы фиатных валютных пар (ЕЦБ-данные, бесплатно) ----------

async def fetch_fx_price_data(base: str, quote: str) -> dict | None:
    """Диапазоны курса за 7/30 дней по официальным дневным курсам ЕЦБ.
    Работает только для пар из двух фиатных валют (не XAU/BTC)."""
    if base in ("XAU", "BTC") or quote in ("XAU", "BTC"):
        return None
    end = date.today()
    start = end - timedelta(days=35)
    url = FRANKFURTER_URL.format(start=start.isoformat(), end=end.isoformat()) + f"?from={base}&to={quote}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            data = await resp.json()

    rates = data.get("rates", {})
    if not rates:
        return None
    sorted_dates = sorted(rates.keys())
    values = [rates[d][quote] for d in sorted_dates if quote in rates[d]]
    if not values:
        return None
    values_7d = values[-7:] if len(values) >= 7 else values

    return {
        "current": values[-1],
        "low_7d": min(values_7d),
        "high_7d": max(values_7d),
        "low_30d": min(values),
        "high_30d": max(values),
    }


def format_fx_raw(data: dict) -> str:
    return (
        f"Курс: {data['current']:.4f}\n"
        f"Диапазон за 7 дней: {data['low_7d']:.4f} – {data['high_7d']:.4f}\n"
        f"Диапазон за 30 дней: {data['low_30d']:.4f} – {data['high_30d']:.4f}"
    )


# ---------- COT: позиционирование крупных трейдеров (CFTC, раз в неделю) ----------

async def fetch_cot_positioning(currency: str) -> dict | None:
    """Данные CFTC по фьючерсам на валюту — во сколько лонгов/шортов сидят
    крупные спекулянты (non-commercial). Обновляется раз в неделю (пятница),
    поэтому кэшируем на сутки. Для USD/XAU/BTC не считается — нет прямого
    фьючерса на "доллар" в этом отчёте."""
    name = COT_CONTRACT_NAMES.get(currency)
    if not name:
        return None

    cached = _cot_cache.get(currency)
    now = datetime.now(timezone.utc)
    if cached and now - cached[0] < COT_CACHE_TTL:
        return cached[1]

    params = {
        "$where": f"market_and_exchange_names like '%{name}%'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "1",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(COT_DATASET_URL, params=params, timeout=15) as resp:
            resp.raise_for_status()
            rows = await resp.json()
    if not rows:
        return None

    row = rows[0]
    try:
        long_pos = int(float(row.get("noncomm_positions_long_all", 0)))
        short_pos = int(float(row.get("noncomm_positions_short_all", 0)))
    except (TypeError, ValueError):
        return None

    result = {
        "date": row.get("report_date_as_yyyy_mm_dd", "")[:10],
        "long": long_pos,
        "short": short_pos,
        "net": long_pos - short_pos,
    }
    _cot_cache[currency] = (now, result)
    return result


def format_cot_raw(currency: str, cot: dict) -> str:
    bias = "нетто-лонг" if cot["net"] > 0 else "нетто-шорт" if cot["net"] < 0 else "нейтрально"
    return (
        f"COT по {currency} ({cot['date']}): крупные спекулянты {bias}, "
        f"лонги {cot['long']:,}, шорты {cot['short']:,}, нетто {cot['net']:+,}"
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

FORECAST_PROMPT = """Ты аналитик форекс, товарных и фондовых рынков. Вот
экономические события на сегодня (High/Medium impact) по основным валютам:
{calendar_summary}

Свежие новостные заголовки:
{headlines}

Дай короткое настроение (бычье/медвежье/нейтральное) по каждой из позиций:
USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD, Золото (XAU), Нефть (WTI/Brent),
DAX 40, Nasdaq, S&P 500.

Формат — строго по одной строке на каждую позицию, на русском:
<эмодзи 📈 или 📉 или ➡️> <Валюта/актив>: <причина в 5-10 слов>

Если по позиции нет значимых факторов сегодня — напиши "нет выраженного драйвера".
Без вступления, без заключения, без дисклеймеров — только список из 13 строк."""

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

INDEX_PROMPT = """Ты аналитик фондового рынка. Вот реальные данные по индексу
{label}:

{raw_data}

Свежие заголовки по рынкам и экономике:
{headlines}

Напиши короткий анализ по-русски (5-7 предложений):
- назови 1-2 зоны поддержки и 1-2 зоны сопротивления (в пунктах индекса) на
  основе диапазонов выше;
- какие факторы сейчас двигают индекс (ставки ФРС/ЕЦБ, отчётности компаний,
  макростатистика, геополитика) — если релевантны заголовки, используй их;
- короткое предположение о вероятном направлении на ближайшие дни;
- заверши фразой, что это не финансовый совет.
Без общих фраз — только конкретика."""

NEWS_DIGEST_PROMPT = """Ты финансовый редактор. Вот сырые заголовки за
последние часы из разных источников (форекс, макро, акции, крипто):

{headlines}

Сделай дайджест по-русски в двух блоках (используй HTML-теги <b> для
заголовков блоков):

<b>Главное</b>
4-6 пунктов через тире — просто перескажи своими словами, что реально
произошло, БЕЗ анализа, только факты по заголовкам (переведи и объедини
похожие).

<b>Что это может значить</b>
2-4 предложения — краткий вывод, как это может повлиять на валюты, индексы,
золото, нефть или крипту.

Если заголовки малозначимы или это в основном шум — так и скажи в конце
коротко."""

PAIR_PROMPT = """Ты аналитик форекс-рынка. Валютная пара: {pair_label}.

События сегодня по {base}: {base_events}
События сегодня по {quote}: {quote_events}

Ценовые данные:
{price_levels}

Позиционирование крупных трейдеров (COT):
{cot_info}

Свежие новостные заголовки:
{headlines}

Напиши короткий анализ по-русски (5-7 предложений):
- какие факторы сейчас двигают эту пару (если факторов нет — так и скажи);
- в чью пользу они складываются ({base} или {quote}), в виде вероятного
  направления пары;
- если есть ценовые диапазоны — назови зону поддержки и зону сопротивления;
- если есть данные COT — упомяни, совпадает ли позиционирование крупных
  игроков с направлением факторов выше или противоречит ему;
- на что обратить внимание в ближайшие часы/дни.
Без общих фраз и дисклеймеров, только суть."""


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


# ---------- Анализ по конкретной валютной паре ----------

def parse_pair(raw: str) -> tuple[str, str] | None:
    """Принимает 'EURUSD', 'EUR/USD', 'eur usd' и т.п., возвращает (base, quote)."""
    cleaned = raw.strip().upper().replace("/", "").replace(" ", "").replace("-", "")
    if len(cleaned) != 6:
        return None
    base, quote = cleaned[:3], cleaned[3:]
    known = MAJOR_CURRENCIES | {"XAU", "BTC"}
    if base not in known or quote not in known:
        return None
    return base, quote


def events_summary_for(events: list[dict], currency: str) -> str:
    relevant = [e for e in events if e.get("country") == currency]
    if not relevant:
        return "нет значимых событий сегодня"
    parts = []
    for e in relevant:
        parts.append(f"{e.get('title')} (прогноз {e.get('forecast') or '—'}, пред. {e.get('previous') or '—'})")
    return "; ".join(parts)


async def build_pair_analysis(base: str, quote: str) -> str:
    pair_label = f"{base}/{quote}"
    try:
        raw_events = await fetch_calendar()
        events = filter_today(raw_events, ("High", "Medium"))
    except Exception:
        events = []

    base_events = events_summary_for(events, base)
    quote_events = events_summary_for(events, quote)

    price_notes = []
    if "BTC" in (base, quote):
        try:
            btc_data = await fetch_btc_market_data()
            price_notes.append(format_btc_raw(btc_data))
        except Exception:
            logger.exception("Не удалось получить данные BTC для анализа пары")
    else:
        try:
            fx_data = await fetch_fx_price_data(base, quote)
            if fx_data:
                price_notes.append(format_fx_raw(fx_data))
        except Exception:
            logger.exception("Не удалось получить курс для пары")

    cot_notes = []
    for ccy in (base, quote):
        try:
            cot = await fetch_cot_positioning(ccy)
        except Exception:
            logger.exception(f"Не удалось получить COT для {ccy}")
            cot = None
        if cot:
            cot_notes.append(format_cot_raw(ccy, cot))

    headlines = fetch_crypto_headlines() if "BTC" in (base, quote) else fetch_recent_headlines()

    price_levels_text = "\n".join(price_notes) or "нет данных по цене"
    cot_text = "\n".join(cot_notes) or "нет данных по позиционированию"

    prompt = PAIR_PROMPT.format(
        pair_label=pair_label,
        base=base,
        quote=quote,
        base_events=base_events,
        quote_events=quote_events,
        price_levels=price_levels_text,
        cot_info=cot_text,
        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
    )
    fallback = (
        f"По {base}: {base_events}\n"
        f"По {quote}: {quote_events}\n"
        f"{price_levels_text}\n"
        f"{cot_text}"
    )
    analysis = await ask_claude(prompt, fallback)
    return f"💱 <b>{pair_label}</b>\n\n{analysis}"


def pairs_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{p[:3]}/{p[3:]}", callback_data=f"pair:{p}")
        for p in POPULAR_PAIRS
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    await message.answer("Строю прогноз по валютам, золоту, нефти и индексам...")
    try:
        raw_events = await fetch_calendar()
    except Exception as e:
        await message.answer(f"Не удалось получить календарь: {e}")
        return
    events = filter_today(raw_events, ("High", "Medium"))
    headlines = fetch_recent_headlines() + fetch_equity_headlines()
    prompt = FORECAST_PROMPT.format(
        calendar_summary=format_calendar_summary(events),
        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
    )
    fallback = format_calendar_summary(events)
    forecast = await ask_claude(prompt, fallback)
    await message.answer(f"🔮 <b>Быстрый прогноз по рынкам</b>\n\n{forecast}", parse_mode="HTML")


@dp.message(Command("btc"))
async def on_btc(message: Message) -> None:
    logger.info(f"/btc от {message.chat.id}")
    await message.answer("Собираю данные по биткоину...")
    try:
        data = await fetch_btc_market_data()
    except Exception as e:
        logger.exception("Ошибка получения данных BTC")
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


@dp.message(Command("pairs"))
async def on_pairs(message: Message) -> None:
    await message.answer("Выбери пару:", reply_markup=pairs_keyboard())


@dp.message(Command("pair"))
async def on_pair(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажи пару, например: <code>/pair EURUSD</code> или <code>/pair GBP/USD</code>.\n"
            "Либо набери /pairs — появятся кнопки.",
            parse_mode="HTML",
        )
        return
    pair = parse_pair(parts[1])
    if pair is None:
        await message.answer("Не распознал пару. Пример: <code>/pair EURUSD</code>", parse_mode="HTML")
        return
    await message.answer(f"Собираю анализ по {pair[0]}/{pair[1]}...")
    text = await build_pair_analysis(*pair)
    await message.answer(text, parse_mode="HTML")


async def build_index_analysis(key: str) -> str:
    asset = INDEX_ASSETS[key]
    data = await fetch_index_data(asset["symbol"])
    raw_data = format_index_raw(asset["label"], data)
    headlines = fetch_equity_headlines() + fetch_recent_headlines(limit=6)
    prompt = INDEX_PROMPT.format(
        label=asset["label"],
        raw_data=raw_data,
        headlines="\n".join(f"- {h}" for h in headlines) or "нет свежих заголовков",
    )
    analysis = await ask_claude(prompt, raw_data)
    return f"📈 <b>{asset['label']}: {data['price']:,.0f} ({data['change_pct']:+.2f}%)</b>\n\n{analysis}"


def indices_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=asset["label"], callback_data=f"index:{key}")
        for key, asset in INDEX_ASSETS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@dp.message(Command("indices"))
async def on_indices(message: Message) -> None:
    await message.answer("Выбери индекс:", reply_markup=indices_keyboard())


@dp.message(Command("index"))
async def on_index(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    key = parts[1].strip().upper().replace(" ", "").replace("&", "") if len(parts) > 1 else ""
    key = {"SP500": "SP500", "S&P500": "SP500", "S&P": "SP500", "DAX": "DAX40", "DAX40": "DAX40",
           "NASDAQ": "NASDAQ"}.get(key)
    if key is None:
        await message.answer(
            "Укажи индекс: <code>/index DAX40</code>, <code>/index NASDAQ</code> или "
            "<code>/index SP500</code>. Либо набери /indices — появятся кнопки.",
            parse_mode="HTML",
        )
        return
    await message.answer(f"Собираю данные по {INDEX_ASSETS[key]['label']}...")
    try:
        text = await build_index_analysis(key)
    except Exception as e:
        logger.exception("Ошибка анализа индекса")
        await message.answer(f"Не удалось получить данные: {e}")
        return
    await message.answer(text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("index:"))
async def on_index_callback(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    await callback.answer()
    if key not in INDEX_ASSETS:
        return
    await callback.message.answer(f"Собираю данные по {INDEX_ASSETS[key]['label']}...")
    try:
        text = await build_index_analysis(key)
    except Exception as e:
        logger.exception("Ошибка анализа индекса")
        await callback.message.answer(f"Не удалось получить данные: {e}")
        return
    await callback.message.answer(text, parse_mode="HTML")


@dp.message(Command("news"))
async def on_news(message: Message) -> None:
    await message.answer("Собираю дайджест новостей...")
    headlines = fetch_all_headlines()
    if not headlines:
        await message.answer("Не удалось получить свежие заголовки.")
        return
    if CLAUDE_ENABLED:
        prompt = NEWS_DIGEST_PROMPT.format(headlines="\n".join(f"- {h}" for h in headlines))
        digest = await ask_claude(prompt, "")
    else:
        translated = translate_to_ru(headlines)
        digest = NO_KEY_NOTICE + "<b>Главное (сырые заголовки)</b>\n" + "\n".join(f"- {h}" for h in translated)
    await message.answer(f"🗞 <b>Дайджест новостей</b>\n\n{digest}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("pair:"))
async def on_pair_callback(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 1)[1]
    pair = parse_pair(code)
    await callback.answer()
    if pair is None:
        return
    await callback.message.answer(f"Собираю анализ по {pair[0]}/{pair[1]}...")
    text = await build_pair_analysis(*pair)
    await callback.message.answer(text, parse_mode="HTML")


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


BOT_COMMANDS = [
    BotCommand(command="start", description="Подписаться и получить сводку на сегодня"),
    BotCommand(command="forecast", description="Быстрый прогноз по валютам, золоту, нефти, индексам"),
    BotCommand(command="pairs", description="Кнопки: анализ по валютной паре"),
    BotCommand(command="pair", description="Анализ по паре текстом, напр. /pair EURUSD"),
    BotCommand(command="indices", description="Кнопки: DAX 40 / Nasdaq / S&P 500"),
    BotCommand(command="index", description="Индекс текстом, напр. /index NASDAQ"),
    BotCommand(command="btc", description="Разбор биткоина: поддержка/сопротивление"),
    BotCommand(command="news", description="Дайджест новостей: главное + что это значит"),
]


async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")
    if not CLAUDE_ENABLED:
        logger.warning("ANTHROPIC_API_KEY не задан — бот работает без AI-анализа, только сырые данные")
    await bot.set_my_commands(BOT_COMMANDS)
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
