import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DB_PATH = os.getenv("DB_PATH", "alerts_bot.sqlite3")
SOURCES_PATH = os.getenv("SOURCES_PATH", "sources.json")
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))
MAX_ITEMS_PER_SOURCE = int(os.getenv("MAX_ITEMS_PER_SOURCE", "12"))
MAX_NOTICES_PER_MESSAGE = int(os.getenv("MAX_NOTICES_PER_MESSAGE", "7"))
STARTUP_PRIME_EXISTING = os.getenv("STARTUP_PRIME_EXISTING", "true").strip().lower() not in {"0", "false", "no", "off"}
REGIONS_PER_PAGE = int(os.getenv("REGIONS_PER_PAGE", "10"))
CITIES_PER_PAGE = int(os.getenv("CITIES_PER_PAGE", "12"))

try:
    LOCAL_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    LOCAL_TZ = timezone.utc

router = Router()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    url: str
    locations: tuple[str, ...]
    channel: str


@dataclass(frozen=True)
class Notice:
    item_id: str
    source_name: str
    source_url: str
    title: str
    text: str
    url: str
    published_at: str | None
    locations: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class Incident:
    key: str
    kind: str
    locations: tuple[str, ...]
    notices: tuple[Notice, ...]


class Setup(StatesGroup):
    region = State()
    city = State()


class RuntimeCache:
    def __init__(self) -> None:
        self.notices: list[Notice] = []
        self.errors: list[str] = []
        self.loaded = False
        self.updated_at: datetime | None = None
        self.lock = asyncio.Lock()

    async def set_data(self, notices: list[Notice], errors: list[str]) -> None:
        async with self.lock:
            self.notices = notices
            self.errors = errors
            self.loaded = True
            self.updated_at = datetime.now(timezone.utc)

    async def snapshot(self) -> tuple[list[Notice], list[str], bool, datetime | None]:
        async with self.lock:
            return list(self.notices), list(self.errors), self.loaded, self.updated_at


cache = RuntimeCache()


def load_config() -> dict[str, Any]:
    with open(SOURCES_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


CONFIG = load_config()
REGIONS = CONFIG["regions"]
GLOBAL_KEYWORDS = [str(value).lower() for value in CONFIG.get("keywords", [])]
CLEAR_KEYWORDS = [str(value).lower() for value in CONFIG.get("clear_keywords", [])]
IGNORE_KEYWORDS = [str(value).lower() for value in CONFIG.get("ignore_keywords", [])]
DANGER_LABELS = CONFIG.get("labels", {})
GLOBAL_SOURCES = CONFIG.get("global_sources", [])
NOTIFY_ALL = "all"
NOTIFY_REGION = "region"
DANGER_KINDS = {"drone", "rocket", "alarm", "clear"}
DRONE_TERMS = [
    "бпла",
    "беспилотник",
    "беспилотники",
    "беспилотный",
    "беспилотные",
    "дрон",
    "дроны",
    "атака дронов",
    "атака беспилотников",
    "атака бпла",
    "угроза атаки бпла",
    "опасность атаки бпла",
    "беспилотная опасность",
    "обнаружен бпла",
    "обнаружены бпла",
    "сбит бпла",
    "сбиты бпла",
    "уничтожен бпла",
    "уничтожены бпла",
    "перехвачен бпла",
    "перехвачены бпла",
    "беспилотных летательных аппаратов",
    "беспилотный летательный аппарат",
]
ROCKET_TERMS = [
    "ракета",
    "ракеты",
    "ракетная опасность",
    "ракетная угроза",
    "ракетная атака",
    "угроза ракетной атаки",
    "ракетный удар",
]
ALARM_TERMS = [
    "красный уровень",
    "красного уровня",
    "желтый уровень",
    "желтого уровня",
    "жёлтый уровень",
    "жёлтого уровня",
    "воздушная опасность",
    "воздушной опасности",
    "режим тревоги",
    "режим опасности",
    "сигнал тревоги",
    "угроза атаки",
    "опасность атаки",
]
CLEAR_TERMS_STRICT = [
    "отбой",
    "отбой красного уровня",
    "отбой желтого уровня",
    "отбой жёлтого уровня",
    "угроза атаки бпла отменена",
    "опасность атаки бпла отменена",
    "воздушная опасность отменена",
    "ракетная опасность отменена",
    "угроза снята",
    "опасность снята",
]
WEATHER_TERMS = [
    "неблагоприятных метеорологических",
    "неблагоприятные метеорологические",
    "метеорологических явлений",
    "метеорологические явления",
    "штормовое предупреждение",
    "гроза",
    "грозой",
    "дожд",
    "ливень",
    "ливни",
    "ветер",
    "ветра",
    "порывы ветра",
    "снег",
    "метель",
    "туман",
    "гололед",
    "гололёд",
    "град",
    "жара",
    "заморозки",
    "шквал",
    "паводок",
]
AVIATION_ONLY_TERMS = [
    "введены временные ограничения",
    "сняты временные ограничения",
    "ограничения на прием",
    "ограничения на приём",
    "ограничения на выпуск",
    "аэропорт",
    "аэропорты",
    "план ковер",
    "план ковёр",
]


def init_db() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            region TEXT NOT NULL,
            city TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(subscribers)").fetchall()}
    if "notify_scope" not in columns:
        db.execute("ALTER TABLE subscribers ADD COLUMN notify_scope TEXT NOT NULL DEFAULT 'all'")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_items (
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (user_id, item_id)
        )
        """
    )
    db.commit()


def normalize(value: str) -> str:
    value = value.replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value.lower(), flags=re.IGNORECASE)
    return " ".join(value.split())


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def keyword_matches(text: str, keyword: str) -> bool:
    haystack = normalize(text)
    needle = normalize(keyword)
    if not needle:
        return False
    if " " in needle:
        return needle in haystack
    tokens = haystack.split()
    if len(needle) <= 3:
        return needle in tokens
    return any(token == needle or token.startswith(needle) for token in tokens)


def text_contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword_matches(text, keyword) for keyword in keywords)


def rough_phrase_match(text: str, phrase: str) -> bool:
    haystack = normalize(text)
    needle = normalize(phrase)
    if not needle:
        return False
    if needle in haystack:
        return True
    tokens = haystack.split()
    words = needle.split()
    if not words:
        return False
    hits = 0
    for word in words:
        if len(word) <= 3:
            found = word in tokens
        else:
            stem = word[: min(7, len(word))]
            found = any(token.startswith(stem) for token in tokens)
        if found:
            hits += 1
    return hits == len(words)


def region_index_by_name(region_name: str) -> int | None:
    normalized = normalize(region_name)
    for index, region in enumerate(REGIONS):
        if normalize(region["name"]) == normalized:
            return index
    return None


def region_by_text(value: str) -> dict[str, Any] | None:
    key = normalize(value)
    for region in REGIONS:
        values = [region["name"], *region.get("aliases", [])]
        if key in {normalize(item) for item in values}:
            return region
    return None


def city_by_text(region: dict[str, Any], value: str) -> str:
    key = normalize(value)
    if key in {"", "-", "нет", "не указывать", "любой", "вся область", "весь регион", "вся москва", "вся республика"}:
        return ""
    for city in region.get("cities", []):
        values = [city["name"], *city.get("aliases", [])]
        if key in {normalize(item) for item in values}:
            return city["name"]
    return compact_text(value)[:80]


def known_city_names(region: dict[str, Any]) -> list[str]:
    return [city["name"] for city in region.get("cities", [])]


def source_from_item(item: dict[str, Any]) -> Source:
    return Source(
        name=str(item["name"]),
        kind=str(item["kind"]),
        url=str(item["url"]),
        locations=tuple(str(value) for value in item.get("locations", [])),
        channel=str(item.get("channel", "")),
    )


def global_sources() -> list[Source]:
    return [source_from_item(item) for item in GLOBAL_SOURCES]


def sources_for_region(region: dict[str, Any]) -> list[Source]:
    return [source_from_item(item) for item in region.get("sources", [])]


def notice_is_ignored(text: str) -> bool:
    return text_contains_any(text, IGNORE_KEYWORDS)


def is_weather_notice(text: str) -> bool:
    return text_contains_any(text, WEATHER_TERMS)


def is_aviation_only_notice(text: str) -> bool:
    if text_contains_any(text, DRONE_TERMS) or text_contains_any(text, ROCKET_TERMS):
        return False
    return text_contains_any(text, AVIATION_ONLY_TERMS)


def has_alarm_context(text: str) -> bool:
    return text_contains_any(text, DRONE_TERMS) or text_contains_any(text, ROCKET_TERMS) or text_contains_any(text, ALARM_TERMS)


def classify_notice(text: str) -> str:
    if is_weather_notice(text):
        return "other"
    if is_aviation_only_notice(text):
        return "other"
    if text_contains_any(text, CLEAR_TERMS_STRICT) and has_alarm_context(text):
        return "clear"
    if text_contains_any(text, DRONE_TERMS):
        return "drone"
    if text_contains_any(text, ROCKET_TERMS):
        return "rocket"
    if text_contains_any(text, ALARM_TERMS):
        return "alarm"
    return "other"


def extract_locations(text: str, source: Source) -> tuple[str, ...]:
    result: list[str] = []
    for location in source.locations:
        if normalize(location) not in {"россия", "рф", "вся россия"}:
            result.append(location)
    for region in REGIONS:
        values = [region["name"], *region.get("aliases", [])]
        if any(rough_phrase_match(text, value) for value in values):
            result.append(region["name"])
        for city in region.get("cities", []):
            city_values = [city["name"], *city.get("aliases", [])]
            if any(rough_phrase_match(text, value) for value in city_values):
                result.append(city["name"])
    seen: set[str] = set()
    unique: list[str] = []
    for item in result:
        key = normalize(item)
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    if not unique:
        for location in source.locations:
            if normalize(location) in {"россия", "рф", "вся россия"}:
                unique.append("Россия")
                break
    return tuple(unique)


def parse_date_bucket(value: str | None) -> str:
    if not value:
        return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except Exception:
        match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", value)
        if match:
            day, month, year = match.groups()
            if len(year) == 2:
                year = "20" + year
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def canonical_text(value: str) -> str:
    value = normalize(value)
    value = re.sub(r"https?\s+\S+", " ", value)
    value = re.sub(r"t me\s+\S+", " ", value)
    value = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " время ", value)
    value = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", " дата ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:1400]


def item_hash(*parts: str) -> str:
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_notice(source: Source, title: str, text: str, url: str, published_at: str | None) -> Notice:
    combined = compact_text(f"{title} {text}")
    kind = classify_notice(combined)
    base_url = url or source.url
    locations = extract_locations(combined, source)
    date_bucket = parse_date_bucket(published_at)
    location_key = ",".join(normalize(value) for value in locations)
    notice_id = item_hash(kind, date_bucket, location_key, canonical_text(combined))
    return Notice(
        item_id=notice_id,
        source_name=source.name,
        source_url=source.url,
        title=compact_text(title)[:500],
        text=compact_text(text)[:2200],
        url=base_url,
        published_at=published_at,
        locations=locations,
        kind=kind,
    )


async def get_text(session: aiohttp.ClientSession, url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 compatible alert-monitor/2.0",
        "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        return await response.text()


async def fetch_rss(session: aiohttp.ClientSession, source: Source) -> list[Notice]:
    raw = await get_text(session, source.url)
    parsed = feedparser.parse(raw)
    notices: list[Notice] = []
    for entry in parsed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = compact_text(getattr(entry, "title", ""))
        summary = compact_text(getattr(entry, "summary", ""))
        link = compact_text(getattr(entry, "link", source.url))
        published = compact_text(getattr(entry, "published", "") or getattr(entry, "updated", "")) or None
        if title or summary:
            notices.append(make_notice(source, title, summary, link, published))
    return notices


async def fetch_telegram(session: aiohttp.ClientSession, source: Source) -> list[Notice]:
    url = source.url
    if source.channel:
        url = f"https://t.me/s/{source.channel.lstrip('@')}"
    raw = await get_text(session, url)
    soup = BeautifulSoup(raw, "html.parser")
    notices: list[Notice] = []
    messages = soup.select(".tgme_widget_message")
    for message in messages[-MAX_ITEMS_PER_SOURCE:]:
        text_node = message.select_one(".tgme_widget_message_text")
        if not text_node:
            continue
        text = compact_text(text_node.get_text(" ", strip=True))
        title = text[:140]
        link_node = message.select_one("a.tgme_widget_message_date")
        link = link_node.get("href", source.url) if link_node else source.url
        time_node = message.select_one("time")
        published = time_node.get("datetime") if time_node else None
        if text:
            notices.append(make_notice(source, title, text, str(link), published))
    return notices


async def fetch_html(session: aiohttp.ClientSession, source: Source) -> list[Notice]:
    raw = await get_text(session, source.url)
    soup = BeautifulSoup(raw, "html.parser")
    notices: list[Notice] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        title = compact_text(link.get_text(" ", strip=True))
        if len(title) < 12:
            continue
        href = str(link.get("href", ""))
        if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        full_url = urljoin(source.url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        parent_text = compact_text(link.parent.get_text(" ", strip=True) if link.parent else title)
        notices.append(make_notice(source, title, parent_text, full_url, None))
        if len(notices) >= MAX_ITEMS_PER_SOURCE:
            break
    return notices


async def fetch_source(session: aiohttp.ClientSession, source: Source) -> list[Notice]:
    if source.kind == "rss":
        return await fetch_rss(session, source)
    if source.kind == "telegram":
        return await fetch_telegram(session, source)
    if source.kind == "html":
        return await fetch_html(session, source)
    raise RuntimeError(f"Неподдерживаемый тип источника: {source.kind}")


def notice_is_danger(notice: Notice) -> bool:
    combined = f"{notice.title} {notice.text}"
    if notice_is_ignored(combined):
        return False
    if is_weather_notice(combined):
        return False
    if is_aviation_only_notice(combined):
        return False
    return notice.kind in DANGER_KINDS


def location_matches(notice: Notice, region: str, city: str) -> bool:
    normalized_region = normalize(region)
    normalized_city = normalize(city)
    locations = {normalize(value) for value in notice.locations}
    if normalized_city and normalized_city in locations:
        return True
    if normalized_region in locations:
        return True
    text = normalize(f"{notice.title} {notice.text} {' '.join(notice.locations)}")
    if normalized_city and rough_phrase_match(text, city):
        return True
    return rough_phrase_match(text, region)


def dedupe_notices(notices: list[Notice]) -> list[Notice]:
    result: list[Notice] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for notice in notices:
        if notice.item_id in seen_ids:
            continue
        text_key = item_hash(notice.kind, ",".join(normalize(value) for value in notice.locations), canonical_text(f"{notice.title} {notice.text}")[:900])
        if text_key in seen_texts:
            continue
        seen_ids.add(notice.item_id)
        seen_texts.add(text_key)
        result.append(notice)
    return result


def filter_notices_for_user(notices: list[Notice], region: str, city: str, notify_scope: str) -> list[Notice]:
    if notify_scope == NOTIFY_ALL:
        result = list(notices)
    else:
        result = [notice for notice in notices if location_matches(notice, region, city)]
    result = dedupe_notices(result)
    return sorted(result, key=lambda item: item.published_at or "", reverse=True)


def scope_label(notify_scope: str) -> str:
    if notify_scope == NOTIFY_REGION:
        return "только выбранный регион"
    return "вся Россия"


def kind_label(kind: str) -> str:
    return str(DANGER_LABELS.get(kind, kind))


def mode_label(kind: str, text: str = "") -> str:
    if kind == "clear":
        return "ОТБОЙ ТРЕВОГИ"
    if kind == "rocket":
        return "РАКЕТНАЯ ОПАСНОСТЬ"
    if kind == "alarm":
        if text_contains_any(text, ["красный уровень", "красного уровня"]):
            return "КРАСНЫЙ РЕЖИМ ТРЕВОГИ"
        if text_contains_any(text, ["желтый уровень", "желтого уровня", "жёлтый уровень", "жёлтого уровня"]):
            return "ЖЁЛТЫЙ РЕЖИМ ТРЕВОГИ"
        if text_contains_any(text, ["воздушная опасность", "воздушной опасности"]):
            return "ВОЗДУШНАЯ ОПАСНОСТЬ"
        return "РЕЖИМ ТРЕВОГИ"
    if kind == "drone":
        if text_contains_any(text, ["угроза атаки бпла", "опасность атаки бпла"]):
            return "УГРОЗА АТАКИ БПЛА"
        if text_contains_any(text, ["атака дронов", "атака беспилотников", "атака бпла"]):
            return "АТАКА БПЛА / ДРОНОВ"
        return "АТАКА БПЛА / ДРОНОВ"
    return kind_label(kind).upper()


def format_time(value: str | None) -> str:
    if not value:
        return "время не указано"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def shorten(value: str, limit: int) -> str:
    value = compact_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def visible_notice_text(notice: Notice) -> str:
    title = compact_text(notice.title)
    text = compact_text(notice.text)
    if text.startswith(title):
        return text
    if title and text:
        return f"{title}. {text}"
    return title or text


def display_locations(notice: Notice) -> str:
    locations = [value for value in notice.locations if normalize(value) not in {"россия", "рф", "вся россия"}]
    if not locations:
        return "Россия"
    return ", ".join(locations[:8]) + (f" и ещё {len(locations) - 8}" if len(locations) > 8 else "")


def strip_urls(value: str) -> str:
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"t\.me/\S+", "", value)
    return compact_text(value)


def strip_public_noise(value: str) -> str:
    value = strip_urls(value)
    value = re.sub(r"@\s*[A-Za-z0-9_]+", "", value)
    value = re.sub(r"@[A-Za-z0-9_]+", "", value)
    cutoff_patterns = [
        r"[❗!]*\s*Радар по всей России\b.*$",
        r"[🌐]*\s*Обход белых списков\b.*$",
        r"\bКанал Минобороны России\b.*$",
        r"\bПодписывай(?:ся|тесь)\b.*$",
        r"\bМЧС России в M(?:A|А)X\b.*$",
        r"\bМЧС России в МАКС\b.*$",
        r"\bМы в M(?:A|А)X\b.*$",
        r"\bМы в МАКС\b.*$",
        r"\bБольше информации тут\b.*$",
        r"\bИнтернет[_ -]?Boost[_ -]?bot\b.*$",
        r"\bInternet[_ -]?Boost[_ -]?bot\b.*$",
    ]
    for pattern in cutoff_patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+[-—]\s*$", "", value)
    return compact_text(value)


def trigger_sentences(text: str) -> list[str]:
    cleaned = strip_public_noise(text)
    parts = [part.strip(" -—•\n\t") for part in re.split(r"(?<=[.!?])\s+|\n+", cleaned) if part.strip()]
    result: list[str] = []
    for part in parts:
        if classify_notice(part) in DANGER_KINDS:
            result.append(part)
    if result:
        return result[:3]
    return parts[:2]


def incident_detail(notices: tuple[Notice, ...]) -> str:
    best = max(notices, key=lambda item: len(visible_notice_text(item)))
    sentences = trigger_sentences(visible_notice_text(best))
    detail = " ".join(sentences)
    if not detail:
        detail = visible_notice_text(best)
    return shorten(detail, 700)


def incident_key(notice: Notice) -> str:
    text = visible_notice_text(notice)
    anchor = " ".join(trigger_sentences(text)) or text
    location_key = ",".join(normalize(value) for value in notice.locations)
    return item_hash(notice.kind, parse_date_bucket(notice.published_at), location_key, canonical_text(anchor)[:900])


def group_incidents(notices: list[Notice]) -> list[Incident]:
    buckets: dict[str, list[Notice]] = {}
    order: list[str] = []
    for notice in dedupe_notices(notices):
        key = incident_key(notice)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(notice)
    incidents: list[Incident] = []
    for key in order:
        items = buckets[key]
        first = items[0]
        locations: list[str] = []
        seen: set[str] = set()
        for item in items:
            for location in item.locations:
                normalized = normalize(location)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    locations.append(location)
        incidents.append(Incident(key=key, kind=first.kind, locations=tuple(locations), notices=tuple(items)))
    return sorted(incidents, key=lambda item: max((notice.published_at or "" for notice in item.notices), default=""), reverse=True)


def incident_locations(incident: Incident) -> str:
    locations = [value for value in incident.locations if normalize(value) not in {"россия", "рф", "вся россия"}]
    if not locations:
        return "Россия"
    return ", ".join(locations[:8]) + (f" и ещё {len(locations) - 8}" if len(locations) > 8 else "")


def incident_time(incident: Incident) -> str:
    value = max((notice.published_at or "" for notice in incident.notices), default="")
    return format_time(value or None)


def incident_title(incident: Incident) -> str:
    detail_text = incident_detail(incident.notices)
    mode = mode_label(incident.kind, detail_text)
    if incident.kind == "clear":
        return "отбой / отмена угрозы"
    if incident.kind == "drone":
        return "атака БПЛА / дронов"
    if incident.kind == "rocket":
        return "ракетная опасность"
    return mode.lower()


def incident_block(incident: Incident, index: int | None = None) -> str:
    detail_text = incident_detail(incident.notices)
    location = incident_locations(incident)
    title = incident_title(incident)
    header = f"{index}. <b>{escape(title)}</b>" if index is not None else f"<b>{escape(title)}</b>"
    return "\n".join([
        header,
        f"Локация: <b>{escape(location)}</b>",
        "",
        f"Время: {escape(incident_time(incident))}",
        escape(detail_text),
    ])


def db_mark_incidents_sent(user_id: int, incidents: list[Incident]) -> None:
    ids: list[str] = []
    for incident in incidents:
        ids.append(f"event:{incident.key}")
        ids.extend(notice.item_id for notice in incident.notices)
    if not ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    db.executemany(
        "INSERT OR IGNORE INTO sent_items (user_id, item_id, sent_at) VALUES (?, ?, ?)",
        [(user_id, item_id, now) for item_id in ids],
    )
    db.commit()


def db_upsert_user(user_id: int, region: str, city: str) -> None:
    db.execute(
        """
        INSERT INTO subscribers (user_id, region, city, notify_scope, is_active, updated_at)
        VALUES (?, ?, ?, 'all', 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            region = excluded.region,
            city = excluded.city,
            is_active = 1,
            updated_at = excluded.updated_at
        """,
        (user_id, region, city, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def db_get_user(user_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT user_id, region, city, notify_scope FROM subscribers WHERE user_id = ? AND is_active = 1",
        (user_id,),
    ).fetchone()


def db_list_users() -> list[sqlite3.Row]:
    return list(db.execute("SELECT user_id, region, city, notify_scope FROM subscribers WHERE is_active = 1").fetchall())


def db_set_notify_scope(user_id: int, notify_scope: str) -> None:
    if notify_scope not in {NOTIFY_ALL, NOTIFY_REGION}:
        notify_scope = NOTIFY_ALL
    db.execute(
        "UPDATE subscribers SET notify_scope = ?, updated_at = ? WHERE user_id = ?",
        (notify_scope, datetime.now(timezone.utc).isoformat(), user_id),
    )
    db.commit()


def db_deactivate_user(user_id: int) -> None:
    db.execute(
        "UPDATE subscribers SET is_active = 0, updated_at = ? WHERE user_id = ?",
        (datetime.now(timezone.utc).isoformat(), user_id),
    )
    db.commit()


def db_mark_sent(user_id: int, item_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO sent_items (user_id, item_id, sent_at) VALUES (?, ?, ?)",
        (user_id, item_id, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def db_was_sent(user_id: int, item_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sent_items WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchone()
    return row is not None


def db_mark_many_sent(user_id: int, notices: list[Notice]) -> None:
    if not notices:
        return
    now = datetime.now(timezone.utc).isoformat()
    db.executemany(
        "INSERT OR IGNORE INTO sent_items (user_id, item_id, sent_at) VALUES (?, ?, ?)",
        [(user_id, notice.item_id, now) for notice in notices],
    )
    db.commit()


def region_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    total = len(REGIONS)
    max_page = max((total - 1) // REGIONS_PER_PAGE, 0)
    page = max(0, min(page, max_page))
    start = page * REGIONS_PER_PAGE
    end = min(start + REGIONS_PER_PAGE, total)
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, end):
        rows.append([InlineKeyboardButton(text=REGIONS[index]["name"], callback_data=f"r:set:{index}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"r:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"r:page:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def city_keyboard(region_index: int, page: int = 0) -> InlineKeyboardMarkup:
    region = REGIONS[region_index]
    cities = known_city_names(region)
    rows: list[list[InlineKeyboardButton]] = [[InlineKeyboardButton(text="Весь регион", callback_data=f"c:all:{region_index}")]]
    if cities:
        max_page = max((len(cities) - 1) // CITIES_PER_PAGE, 0)
        page = max(0, min(page, max_page))
        start = page * CITIES_PER_PAGE
        end = min(start + CITIES_PER_PAGE, len(cities))
        for index in range(start, end):
            rows.append([InlineKeyboardButton(text=cities[index], callback_data=f"c:set:{region_index}:{index}")])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"c:page:{region_index}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"c:page:{region_index}:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Ввести город вручную", callback_data=f"c:manual:{region_index}")])
    rows.append([InlineKeyboardButton(text="Назад к регионам", callback_data="r:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_actions_keyboard(notify_scope: str) -> InlineKeyboardMarkup:
    all_text = "✅ Вся Россия" if notify_scope == NOTIFY_ALL else "Вся Россия"
    region_text = "✅ Только мой регион" if notify_scope == NOTIFY_REGION else "Только мой регион"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=all_text, callback_data="notify:all")],
            [InlineKeyboardButton(text=region_text, callback_data="notify:region")],
            [InlineKeyboardButton(text="Изменить регион и город", callback_data="setup:start")],
            [InlineKeyboardButton(text="Обновить статус", callback_data="status:show")],
        ]
    )


def build_digest_message(region: str, city: str, notify_scope: str, incidents: list[Incident], hidden_count: int = 0) -> str:
    lines: list[str] = ["<b>мониторинг атак РФ</b>", f"Режим: <b>{escape(scope_label(notify_scope))}</b>", ""]
    for index, incident in enumerate(incidents, start=1):
        if index > 1:
            lines.append("")
        lines.append(incident_block(incident, index))
    if hidden_count > 0:
        lines.append("")
        lines.append(f"Ещё {hidden_count} событий скрыто в этом цикле, чтобы не спамить.")
    return "\n".join(lines).strip()


def build_status_message(region: str, city: str, notify_scope: str, notices: list[Notice], errors: list[str], loaded: bool, updated_at: datetime | None) -> str:
    location = region if not city else f"{region}, {city}"
    lines = [f"Подписка: <b>{escape(location)}</b>", f"Режим уведомлений: <b>{escape(scope_label(notify_scope))}</b>"]
    if not loaded:
        lines.append("")
        lines.append("Данные еще не загружены.")
        return "\n".join(lines)
    incidents = group_incidents(notices)
    lines.append("")
    if incidents:
        lines.append("Последние найденные события по БПЛА/ракетным угрозам:")
        for index, incident in enumerate(incidents[:MAX_NOTICES_PER_MESSAGE], start=1):
            lines.append("")
            lines.append(incident_block(incident, index))
    else:
        if notify_scope == NOTIFY_REGION:
            lines.append("По выбранному региону событий БПЛА/ракетных угроз не найдено.")
        else:
            lines.append("Событий БПЛА/ракетных угроз в последних публикациях не найдено.")
    if updated_at:
        lines.append("")
        lines.append(f"Проверено ботом: {escape(updated_at.astimezone(LOCAL_TZ).strftime('%d.%m.%Y %H:%M'))}")
    return "\n".join(lines)

def build_sources_message() -> str:
    return "Источники скрыты в пользовательском выводе. Бот показывает только события БПЛА/дронов/ракет и отбои."


async def send_long(bot: Bot, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    limit = 3500
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    for index, part in enumerate(parts):
        markup = reply_markup if index == len(parts) - 1 else None
        await bot.send_message(user_id, part, reply_markup=markup)


async def refresh_sources() -> None:
    errors: list[str] = []
    notices: list[Notice] = []
    all_sources: list[Source] = []
    all_sources.extend(global_sources())
    for region in REGIONS:
        all_sources.extend(sources_for_region(region))
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_source(session, source) for source in all_sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for source, result in zip(all_sources, results):
        if isinstance(result, Exception):
            errors.append(f"{source.name}: {result}")
            continue
        for notice in result:
            if notice_is_danger(notice):
                notices.append(notice)
    unique: dict[str, Notice] = {}
    for notice in dedupe_notices(notices):
        unique[notice.item_id] = notice
    await cache.set_data(list(unique.values()), errors)


async def prime_existing_users() -> None:
    notices, _, loaded, _ = await cache.snapshot()
    if not loaded:
        return
    for user in db_list_users():
        region = str(user["region"])
        city = str(user["city"] or "")
        notify_scope = str(user["notify_scope"] or NOTIFY_ALL)
        matched = filter_notices_for_user(notices, region, city, notify_scope)
        db_mark_incidents_sent(int(user["user_id"]), group_incidents(matched))


async def notify_users(bot: Bot) -> None:
    notices, _, loaded, _ = await cache.snapshot()
    if not loaded:
        return
    for user in db_list_users():
        user_id = int(user["user_id"])
        region = str(user["region"])
        city = str(user["city"] or "")
        notify_scope = str(user["notify_scope"] or NOTIFY_ALL)
        matched = filter_notices_for_user(notices, region, city, notify_scope)
        incidents = group_incidents(matched)
        new_incidents = [incident for incident in incidents if not db_was_sent(user_id, f"event:{incident.key}")]
        if not new_incidents:
            continue
        visible = new_incidents[:MAX_NOTICES_PER_MESSAGE]
        hidden = max(0, len(new_incidents) - len(visible))
        try:
            await send_long(
                bot,
                user_id,
                build_digest_message(region, city, notify_scope, visible, hidden),
                reply_markup=user_actions_keyboard(notify_scope),
            )
            db_mark_incidents_sent(user_id, new_incidents)
        except TelegramForbiddenError:
            db_deactivate_user(user_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramAPIError:
            logging.exception("Ошибка отправки пользователю %s", user_id)


async def monitor_loop(bot: Bot) -> None:
    while True:
        try:
            await refresh_sources()
            await notify_users(bot)
        except Exception:
            logging.exception("Ошибка цикла мониторинга")
        await asyncio.sleep(max(POLL_INTERVAL_SECONDS, 30))


async def send_region_menu(bot: Bot, chat_id: int, state: FSMContext, page: int = 0) -> None:
    await state.set_state(Setup.region)
    await bot.send_message(chat_id, "Выберите регион:", reply_markup=region_keyboard(page))


async def send_city_menu(bot: Bot, chat_id: int, state: FSMContext, region_index: int, page: int = 0) -> None:
    region = REGIONS[region_index]
    await state.update_data(region=region["name"])
    await state.set_state(Setup.city)
    await bot.send_message(chat_id, f"Выберите город для региона: <b>{escape(region['name'])}</b>", reply_markup=city_keyboard(region_index, page))


async def complete_setup(bot: Bot, chat_id: int, user_id: int, state: FSMContext, region: dict[str, Any], city: str) -> None:
    db_upsert_user(user_id, region["name"], city)
    notices, errors, loaded, updated_at = await cache.snapshot()
    user = db_get_user(user_id)
    notify_scope = str(user["notify_scope"] or NOTIFY_ALL) if user else NOTIFY_ALL
    matched = filter_notices_for_user(notices, region["name"], city, notify_scope)
    db_mark_incidents_sent(user_id, group_incidents(matched))
    await state.clear()
    await bot.send_message(chat_id, "Настройка сохранена. Новые совпадения будут приходить одним сводным сообщением.", reply_markup=ReplyKeyboardRemove())
    await send_long(
        bot,
        chat_id,
        build_status_message(region["name"], city, notify_scope, matched, errors, loaded, updated_at),
        reply_markup=user_actions_keyboard(notify_scope),
    )


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user and db_get_user(message.from_user.id):
        await send_status_screen(message.bot, message.chat.id, message.from_user.id)
        return
    await send_region_menu(message.bot, message.chat.id, state, 0)


@router.message(Command("change"))
async def on_change(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_region_menu(message.bot, message.chat.id, state, 0)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(
        "/start — открыть настройки\n"
        "/status — открыть статус и inline-настройки\n"
        "/change — изменить регион и город\n"
        "/stop — отключить уведомления"
    )


async def send_status_screen(bot: Bot, chat_id: int, user_id: int) -> None:
    user = db_get_user(user_id)
    if not user:
        await bot.send_message(chat_id, "Сначала выберите регион и город через /start.")
        return
    notices, errors, loaded, updated_at = await cache.snapshot()
    notify_scope = str(user["notify_scope"] or NOTIFY_ALL)
    matched = filter_notices_for_user(notices, str(user["region"]), str(user["city"] or ""), notify_scope)
    await send_long(
        bot,
        chat_id,
        build_status_message(str(user["region"]), str(user["city"] or ""), notify_scope, matched, errors, loaded, updated_at),
        reply_markup=user_actions_keyboard(notify_scope),
    )


@router.callback_query(F.data == "noop")
async def on_noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("notify:"))
async def on_notify_callback(callback: CallbackQuery) -> None:
    if not callback.from_user:
        return
    user = db_get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала выберите регион и город через /start.", show_alert=True)
        return
    value = str(callback.data or "").split(":", 1)[1]
    notify_scope = NOTIFY_REGION if value == NOTIFY_REGION else NOTIFY_ALL
    db_set_notify_scope(callback.from_user.id, notify_scope)
    notices, _, loaded, _ = await cache.snapshot()
    if loaded:
        matched = filter_notices_for_user(notices, str(user["region"]), str(user["city"] or ""), notify_scope)
        db_mark_incidents_sent(callback.from_user.id, group_incidents(matched))
    await callback.answer("Сохранено")
    if callback.message:
        await send_status_screen(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "status:show")
async def on_status_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        return
    await callback.answer("Обновляю статус")
    await send_status_screen(callback.bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "setup:start")
async def on_setup_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    await state.clear()
    await callback.answer()
    await send_region_menu(callback.bot, callback.message.chat.id, state, 0)


@router.callback_query(F.data.startswith("r:page:"))
async def on_region_page_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    page = int(str(callback.data).split(":")[2])
    await state.set_state(Setup.region)
    await callback.answer()
    await callback.message.edit_text("Выберите регион:", reply_markup=region_keyboard(page))


@router.callback_query(F.data.startswith("r:set:"))
async def on_region_set_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    region_index = int(str(callback.data).split(":")[2])
    await callback.answer()
    await send_city_menu(callback.bot, callback.message.chat.id, state, region_index, 0)


@router.callback_query(F.data.startswith("c:page:"))
async def on_city_page_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    _, _, region_index_raw, page_raw = str(callback.data).split(":")
    region_index = int(region_index_raw)
    page = int(page_raw)
    await callback.answer()
    await callback.message.edit_text(
        f"Выберите город для региона: <b>{escape(REGIONS[region_index]['name'])}</b>",
        reply_markup=city_keyboard(region_index, page),
    )
    await state.update_data(region=REGIONS[region_index]["name"])
    await state.set_state(Setup.city)


@router.callback_query(F.data.startswith("c:all:"))
async def on_city_all_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    region_index = int(str(callback.data).split(":")[2])
    await callback.answer()
    await complete_setup(callback.bot, callback.message.chat.id, callback.from_user.id, state, REGIONS[region_index], "")


@router.callback_query(F.data.startswith("c:set:"))
async def on_city_set_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    _, _, region_index_raw, city_index_raw = str(callback.data).split(":")
    region_index = int(region_index_raw)
    city_index = int(city_index_raw)
    city = known_city_names(REGIONS[region_index])[city_index]
    await callback.answer()
    await complete_setup(callback.bot, callback.message.chat.id, callback.from_user.id, state, REGIONS[region_index], city)


@router.callback_query(F.data.startswith("c:manual:"))
async def on_city_manual_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    region_index = int(str(callback.data).split(":")[2])
    await state.update_data(region=REGIONS[region_index]["name"])
    await state.set_state(Setup.city)
    await callback.answer()
    await callback.message.answer("Напишите название города вручную.")


@router.message(Command("sources"))
async def on_sources(message: Message) -> None:
    await send_long(message.bot, message.chat.id, build_sources_message())


@router.message(Command("stop"))
async def on_stop(message: Message, state: FSMContext) -> None:
    if message.from_user:
        db_deactivate_user(message.from_user.id)
    await state.clear()
    await message.answer("Уведомления отключены.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    if not message.from_user:
        return
    await send_status_screen(message.bot, message.chat.id, message.from_user.id)


@router.message(Setup.region)
async def process_region(message: Message, state: FSMContext) -> None:
    region = region_by_text(message.text or "")
    if not region:
        await message.answer("Регион не найден. Выберите регион inline-кнопкой или напишите точное название.", reply_markup=region_keyboard(0))
        return
    region_index = region_index_by_name(region["name"])
    if region_index is None:
        await message.answer("Регион не найден в конфиге.")
        return
    await send_city_menu(message.bot, message.chat.id, state, region_index, 0)


@router.message(Setup.city)
async def process_city(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    data = await state.get_data()
    region = region_by_text(str(data.get("region", "")))
    if not region:
        await state.clear()
        await message.answer("Ошибка состояния. Запустите /start заново.", reply_markup=ReplyKeyboardRemove())
        return
    city = city_by_text(region, message.text or "")
    await complete_setup(message.bot, message.chat.id, message.from_user.id, state, region, city)


@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Команда не распознана. Используйте /start или /status.")


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    logging.basicConfig(level=logging.INFO)
    init_db()
    await refresh_sources()
    if STARTUP_PRIME_EXISTING:
        await prime_existing_users()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    task = asyncio.create_task(monitor_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
