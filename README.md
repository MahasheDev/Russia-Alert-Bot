# RF Alert Bot

Telegram-бот для мониторинга сообщений об атаках БПЛА, дронов, ракетной опасности, режимах тревоги и отбоях тревоги по регионам России.

Бот собирает сообщения из заданных публичных источников, фильтрует нерелевантные публикации и отправляет пользователям краткие уведомления в Telegram.

## Возможности

- мониторинг сообщений по всей России;
- режим уведомлений только по выбранному региону;
- выбор региона и города через inline-кнопки;
- постоянные inline-кнопки управления под сообщениями;
- фильтрация погодных, рекламных, служебных и нерелевантных публикаций;
- дедубликация похожих сообщений из разных источников;
- хранение пользователей и истории отправленных событий в SQLite;
- автоматический запуск через systemd.

## Что отслеживается

Бот ориентирован только на сообщения, связанные с:

- атаками БПЛА;
- атаками дронов;
- ракетной опасностью;
- воздушной опасностью;
- красным, жёлтым и другими режимами тревоги;
- отбоями тревоги.

Погодные предупреждения, грозы, дождь, ветер, общие новости, рекламные посты и служебные объявления не должны попадать в уведомления.

## Формат уведомлений

Пример сообщения:

```text
мониторинг атак РФ
Режим: вся Россия

1. атака БПЛА / дронов
Локация: Липецкая область

Время: 23.05.2026 15:05
Липецкая область. Объявлен красный уровень опасности по БПЛА.
```

Источники и ссылки в пользовательских уведомлениях не выводятся.

## Команды бота

```text
/start — выбор региона и города
/status — текущий статус и кнопки управления
/sources — список подключенных источников
/change — изменить регион и город
/stop — отключить уведомления
```

Основные настройки уведомлений доступны через inline-кнопки:

```text
Вся Россия
Только мой регион
Изменить регион и город
Обновить статус
```

## Требования

- Python 3.10 или новее;
- Telegram-бот, созданный через BotFather;
- Linux-сервер или VPS;
- systemd для автозапуска.

## Установка

```bash
cd /root/tgbots/tatarstanalertbot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

## Настройка `.env`

```env
TELEGRAM_BOT_TOKEN=1234567890:replace_me
POLL_INTERVAL_SECONDS=60
DB_PATH=alerts_bot.sqlite3
SOURCES_PATH=sources.json
HTTP_TIMEOUT_SECONDS=20
MAX_ITEMS_PER_SOURCE=12
MAX_NOTICES_PER_MESSAGE=7
STARTUP_PRIME_EXISTING=true
```

Параметры:

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен Telegram-бота |
| `POLL_INTERVAL_SECONDS` | интервал проверки источников |
| `DB_PATH` | путь к SQLite-базе |
| `SOURCES_PATH` | путь к файлу источников |
| `HTTP_TIMEOUT_SECONDS` | таймаут HTTP-запросов |
| `MAX_ITEMS_PER_SOURCE` | сколько последних сообщений брать из одного источника |
| `MAX_NOTICES_PER_MESSAGE` | максимум событий в одном уведомлении |
| `STARTUP_PRIME_EXISTING` | помечать старые найденные события как обработанные при запуске |

## Запуск вручную

```bash
cd /root/tgbots/tatarstanalertbot
source venv/bin/activate
python3 bot.py
```

## Автозапуск через systemd

```bash
BOT_DIR="/root/tgbots/tatarstanalertbot"
SERVICE_NAME="rfalertbot"

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=RF Alert Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${BOT_DIR}/.env
ExecStart=${BOT_DIR}/venv/bin/python ${BOT_DIR}/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager
```

## Управление службой

```bash
sudo systemctl restart rfalertbot
sudo systemctl stop rfalertbot
sudo systemctl status rfalertbot --no-pager
```

Логи:

```bash
sudo journalctl -u rfalertbot -f
```

## Проверка перед запуском

```bash
cd /root/tgbots/tatarstanalertbot
source venv/bin/activate
python3 -m py_compile bot.py
python3 bot.py
```

## Источники

Источники задаются в файле `sources.json`.

Каждый источник содержит:

```json
{
  "name": "Название источника",
  "kind": "telegram",
  "url": "https://t.me/s/channel",
  "channel": "channel",
  "locations": ["Россия"]
}
```

Поддерживаемые типы источников:

| Тип | Описание |
|---|---|
| `telegram` | публичная Telegram-лента через `t.me/s/...` |
| `rss` | RSS-лента |
| `html` | HTML-страница |

## База данных

Бот использует SQLite.

По умолчанию база хранится в файле:

```text
alerts_bot.sqlite3
```

В базе хранятся:

- подписчики;
- выбранные регионы и города;
- режим уведомлений;
- уже отправленные события.

## Структура проекта

```text
.
├── bot.py
├── sources.json
├── requirements.txt
├── .env.example
├── README.md
└── alerts_bot.sqlite3
```

## Безопасность

Бот не является официальной системой оповещения и не должен использоваться как единственный источник информации при угрозах. При получении уведомления нужно сверяться с официальными каналами и выполнять указания служб.
