#!/usr/bin/env python3
"""Еженедельная сводка ожидаемых поступлений: МойСклад (только GET) -> чат YouGile."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
LOG = logging.getLogger("weekly_receipts")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[-–]\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)"
)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SKIP_DATE_WORDS = ("готовност", "производств", "дата выхода", "дата отгрузки", "оплат", "старт")
ARRIVAL_WORDS = ("ожида", "прибыт", "прибуд", "приход", "достав", "в москв", "в мск")
MOYSKLAD_BASE = "https://api.moysklad.ru/api/remap/1.2"
YOUGILE_BASE = "https://yougile.com/api-v2"
TASK_CODE = "PRO-10131"


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_window(day: date) -> tuple[date, date]:
    monday = monday_of(day)
    return monday, monday + timedelta(days=6)


def fmt_qty(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount.normalize(), "f").replace(".", ",")


def number(value: object) -> Decimal:
    return Decimal(str(value or 0))


def parse_day(day: str, month: str, year: str | None, base: date) -> date | None:
    try:
        if year:
            yr = int(year)
            if yr < 100:
                yr += 2000
            return date(yr, int(month), int(day))
        options = [date(yr, int(month), int(day)) for yr in (base.year - 1, base.year, base.year + 1)]
        return min(options, key=lambda candidate: abs((candidate - base).days))
    except ValueError:
        return None


def arrival_candidates(description: str, base: date) -> list[tuple[int, int, date, date, str]]:
    """Даты прихода, а не оплаты/завершения производства. Приоритет у 'ожидаем'."""
    result = []
    for index, line in enumerate((description or "").splitlines()):
        lowered = line.casefold()
        if not any(word in lowered for word in ARRIVAL_WORDS):
            continue
        if any(word in lowered for word in SKIP_DATE_WORDS):
            continue
        priority = 3 if "ожида" in lowered else 2
        if "прибыт" in lowered or "в мск" in lowered:
            priority += 1
        matches = list(RANGE_RE.finditer(line))
        consumed = []
        for match in matches:
            first, last, month, yr = match.groups()
            start = parse_day(first, month, yr, base)
            end = parse_day(last, month, yr, base)
            if start and end and start <= end and (end - start).days <= 14:
                consumed.append(match.span())
                result.append((priority, index, start, end, line.strip()))
        for match in DATE_RE.finditer(line):
            if any(a <= match.start() < b for a, b in consumed):
                continue
            day = parse_day(*match.groups(), base)
            if day:
                result.append((priority, index, day, day, line.strip()))
    return result


def planned_arrival(order: dict, week_start: date, week_end: date):
    """Выбрать последнюю релевантную дату в комментарии. Даты вне недели не подменять."""
    possible = arrival_candidates(order.get("description") or "", week_start)
    # Самые последние (в тексте) строки заметок выше структурного поля.
    # Если даты по разным партиям, показать все актуальные даты и примечание.
    if possible:
        # Даты в описании часто меняют по мере перевозки. Сначала ищем
        # последнюю актуальную пометку с наивысшим приоритетом — даже если
        # она перенесла поставку за пределы запрашиваемой недели.
        best_priority = max(item[0] for item in possible)
        top = [item for item in possible if item[0] == best_priority]
        winner = top[-1]
        return winner[2], winner[3], "комментарий", winner[4], len(top) > 1
    raw = (order.get("deliveryPlannedMoment") or "").strip()
    try:
        planned = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None
    return planned, planned, "плановая дата МС", "", False


def as_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("Непредвиденный ответ API: ожидался список/объект")
    for key in ("rows", "content", "items", "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise ValueError("API не вернул распознаваемый список")


class Api:
    def __init__(self, base: str, token: str, *, timeout: int = 25):
        if not token:
            raise ValueError("Не заполнен API-ключ")
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, path: str, *, params: dict | None = None, body: dict | None = None):
        url = self.base + "/" + path.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        method = "POST" if body is not None else "GET"
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        for attempt in range(4):
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                    raw = reply.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3 and method == "GET":
                    time.sleep(min(2 ** attempt, 8))
                    continue
                # Тела ошибок не печатаем: возможна конфиденциальная информация.
                raise RuntimeError(f"API {method} {path}: HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                if attempt < 3 and method == "GET":
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"API недоступен ({path})") from exc
        raise RuntimeError("Не удалось выполнить запрос")

    def pages(self, path: str, *, params: dict | None = None, limit: int = 100, max_pages: int = 120):
        for page in range(max_pages):
            data = self.request(path, params={**(params or {}), "limit": limit, "offset": page * limit})
            rows = as_rows(data)
            for row in rows:
                yield row
            if len(rows) < limit:
                return
        raise RuntimeError("Превышен предел страниц API — отчет не будет отправлен")


def obj_id(meta_entity: dict) -> str | None:
    if not isinstance(meta_entity, dict):
        return None
    identifier = meta_entity.get("id")
    if isinstance(identifier, str) and UUID_RE.fullmatch(identifier):
        return identifier
    href = (meta_entity.get("meta") or {}).get("href") or ""
    match = UUID_RE.search(href)
    return match.group(0) if match else None


def assortment_key(row: dict) -> str | None:
    return obj_id(row.get("assortment") or row.get("product") or {})


def get_positions(ms: Api, path: str) -> list[dict]:
    return list(ms.pages(path, params={"expand": "assortment"}))


def accepted_quantities(ms: Api, order: dict) -> tuple[dict[str, Decimal], bool]:
    """Только позиции проведенных приемок. Если связи есть — они обязательны к проверке."""
    receipt_refs = order.get("supplies") or []
    accepted = defaultdict(lambda: Decimal("0"))
    if not receipt_refs:
        return accepted, False
    for reference in receipt_refs:
        receipt_id = obj_id(reference)
        if not receipt_id:
            raise RuntimeError(f"Не удалось прочитать ссылку на приемку заказа {order.get('name')}")
        receipt = ms.request(f"entity/supply/{receipt_id}")
        if receipt.get("applicable") is not True:
            continue
        for item in get_positions(ms, f"entity/supply/{receipt_id}/positions"):
            key = assortment_key(item)
            if key:
                accepted[key] += number(item.get("quantity"))
    return accepted, True


def pending_products(ms: Api, order: dict) -> list[dict]:
    positions = get_positions(ms, f"entity/purchaseorder/{order['id']}/positions")
    accepted, has_receipts = accepted_quantities(ms, order)
    products = []
    for position in positions:
        product = position.get("assortment") or {}
        # Услуги и неидентифицированные позиции не считаем товаром.
        if not isinstance(product, dict):
            continue
        quantity = number(position.get("quantity"))
        key = assortment_key(position)
        # В API заказа поле shipped — уже принятое количество.
        # При наличии связей с приёмками используем только проведённые приёмки.
        arrived = accepted[key] if has_receipts and key else number(position.get("shipped"))
        pending = max(Decimal("0"), quantity - arrived)
        if pending <= 0:
            continue
        sku = str(product.get("article") or product.get("code") or "").strip()
        name = str(product.get("name") or "Товар без названия").strip()
        # Для позиций без артикула используем наименование, не скрывая их.
        products.append({"sku": sku or "—", "name": name, "quantity": pending})
    return products


def collect(ms: Api, week_start: date, week_end: date) -> list[dict]:
    arrivals = []
    scanned = 0
    for summary in ms.pages("entity/purchaseorder", params={"order": "moment,desc"}):
        scanned += 1
        # Документы с полной приемкой можно пропустить. Частичную НЕ пропускаем.
        if (number(summary.get("sum")) > 0
                and number(summary.get("shippedSum")) >= number(summary.get("sum"))):
            continue
        selected = planned_arrival(summary, week_start, week_end)
        if selected is None:
            continue
        start, end, source, note, multiple_dates = selected
        if end < week_start or start > week_end:
            continue
        full = ms.request(f"entity/purchaseorder/{summary['id']}")
        # Проверяем обновленный комментарий и дату повторно: список мог устареть.
        chosen = planned_arrival(full, week_start, week_end)
        if not chosen:
            continue
        start, end, source, note, multiple_dates = chosen
        if end < week_start or start > week_end:
            continue
        goods = pending_products(ms, full)
        if not goods:
            continue
        arrivals.append({
            "name": full.get("name") or summary.get("name") or "?",
            "date": start,
            "date_end": end,
            "source": source,
            "note": note,
            "multiple_dates": multiple_dates,
            "supplier": (full.get("agent") or {}).get("name", ""),
            "products": goods,
        })
    LOG.info("Просмотрено %s заказов; с ожидаемыми товарами на неделю: %s", scanned, len(arrivals))
    return sorted(arrivals, key=lambda o: (o["date"], o["name"]))


def label_date(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d.%m")
    if start.month == end.month and start.year == end.year:
        return f"{start.day:02d}–{end.day:02d}.{start.month:02d}"
    return f"{start:%d.%m}–{end:%d.%m}"


def format_message(arrivals: list[dict], start: date, end: date) -> str:
    marker = f"[MS-WEEKLY:{start.isoformat()}]"
    lines = [
        f"📦 ПОСТУПЛЕНИЯ НА НЕДЕЛЮ · {start:%d.%m}–{end:%d.%m.%Y}",
        marker,
        "План прибытия на МС, НЕ дата наличия на маркетплейсах.",
        "",
    ]
    if not arrivals:
        lines += ["Заказов с ожидаемыми неполученными товарами на эту неделю не найдено."]
    else:
        for order in arrivals:
            label = label_date(order["date"], order["date_end"])
            tentative = " ≈" if (order["source"] == "плановая дата МС"
                                 or "±" in order["note"]
                                 or "+-" in order["note"]
                                 or order["date"] != order["date_end"]) else ""
            lines.append(f"📅 {label}{tentative} · Заказ №{order['name']}")
            if order["supplier"]:
                lines.append(f"Поставщик: {order['supplier']}")
            for item in order["products"]:
                lines.append(f"  • {item['sku']} — {item['name']}: {fmt_qty(item['quantity'])} шт.")
            if order["source"] == "плановая дата МС":
                lines.append("  ⚠ Дата только из поля плана МС; в описании нет точного ожидания.")
            if order["multiple_dates"]:
                lines.append("  ⚠ В комментарии несколько дат партий — распределение по SKU уточнить.")
            if order["note"] and order["source"] == "комментарий":
                compact = " ".join(order["note"].split())[:180]
                lines.append(f"  Примечание МС: {compact}")
            lines.append("")
    lines.append("Приёмки учтены; дата ориентировочная до фактического подтверждения.")
    message = "\n".join(lines)
    if len(message) > 25000:
        raise RuntimeError("Сводка слишком длинная; сообщение не отправлено, чтобы не потерять позиции")
    return message


def resolve_chat_id(yg: Api, explicit: str, code: str) -> str:
    if explicit:
        if not UUID_RE.fullmatch(explicit):
            raise ValueError("YOUGILE_TASK_UUID должен быть UUID задачи, а не номером PRO-10131")
        return explicit
    # Пользовательская ссылка содержит человекочитаемый код, а API ожидает UUID.
    # Без токена/доступа задача не распознается; при отсутствии точного совпадения не шлём.
    wanted = code.strip().upper().lstrip("#")
    for task in yg.pages("task-list", limit=100, max_pages=150):
        for field in ("code", "taskCode", "shortId", "publicId", "displayId", "key", "idCode"):
            value = task.get(field)
            if value is not None and str(value).upper().lstrip("#") == wanted:
                task_id = obj_id(task)
                if task_id:
                    return task_id
    raise RuntimeError(
        f"Не удалось найти UUID для задачи {wanted} через API. "
        "Укажите YOUGILE_TASK_UUID в GitHub Secrets; отправка отменена."
    )


def chat_history(yg: Api, chat_id: str, max_pages: int = 10) -> list[dict]:
    history = []
    for index in range(max_pages):
        response = yg.request(f"chats/{chat_id}/messages", params={"limit": 100, "offset": index * 100})
        items = as_rows(response)
        history.extend(items)
        if len(items) < 100:
            break
    return history


def already_sent(history: list[dict], start: date) -> bool:
    marker = f"[MS-WEEKLY:{start.isoformat()}]"
    return any(marker in str(message.get("text", "")) or marker in str(message.get("textHtml", ""))
               for message in history)


def send_message(yg: Api, chat_id: str, message: str):
    # Чат задачи использует UUID задачи как chatId по REST API YouGile v2.
    return yg.request(
        f"chats/{chat_id}/messages",
        body={
            "text": message,
            "textHtml": "<p>" + html.escape(message).replace("\n", "<br>") + "</p>",
        },
    )


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Без отправки в YouGile")
    parser.add_argument("--date", help="Дата расчета YYYY-MM-DD, по умолчанию сегодня МСК")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    today = date.fromisoformat(args.date) if args.date else datetime.now(MOSCOW).date()
    start, end = week_window(today)
    token = os.environ.get("MOYSKLAD_TOKEN", "")
    ms = Api(os.environ.get("MOYSKLAD_API_URL", MOYSKLAD_BASE), token)
    arrivals = collect(ms, start, end)
    message = format_message(arrivals, start, end)
    # В Actions передаем полный отчет только на ручной dry-run (не в автоматические логи).
    if args.dry_run:
        print(message)
        return
    yg = Api(os.environ.get("YOUGILE_API_URL", YOUGILE_BASE), os.environ.get("YOUGILE_TOKEN", ""))
    chat_id = resolve_chat_id(
        yg, os.environ.get("YOUGILE_TASK_UUID", ""), os.environ.get("YOUGILE_TASK_CODE", TASK_CODE)
    )
    if already_sent(chat_history(yg, chat_id), start):
        LOG.info("Сводка за %s уже отправлялась: повторная публикация отменена", start)
        return
    send_message(yg, chat_id, message)
    LOG.info("Сводка за %s–%s отправлена в YouGile", start, end)


if __name__ == "__main__":
    try:
        run()
    except (RuntimeError, ValueError, urllib.error.URLError) as error:
        LOG.error("%s", error)
        sys.exit(1)
