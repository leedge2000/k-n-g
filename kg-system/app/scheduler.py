from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .crawler import Crawler

logger = logging.getLogger(__name__)


def _cron_values(expression: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for item in expression.split(","):
        base, separator, step_text = item.partition("/")
        step = int(step_text) if separator else 1
        if step < 1:
            raise ValueError("cron 步长必须大于 0")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron 字段超出范围：{item}")
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expression: str, moment: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("仅支持五段 cron 表达式")
    minute, hour, day_of_month, month, day_of_week = fields
    cron_weekday = (moment.weekday() + 1) % 7
    weekdays = _cron_values(day_of_week, 0, 7)
    if 7 in weekdays:
        weekdays.add(0)
    basic_match = (
        moment.minute in _cron_values(minute, 0, 59)
        and moment.hour in _cron_values(hour, 0, 23)
        and moment.month in _cron_values(month, 1, 12)
    )
    month_day_match = moment.day in _cron_values(day_of_month, 1, 31)
    week_day_match = cron_weekday in weekdays
    if day_of_month != "*" and day_of_week != "*":
        day_match = month_day_match or week_day_match
    else:
        day_match = month_day_match and week_day_match
    return basic_match and day_match


class CrawlScheduler:
    def __init__(self, crawler: Crawler, timezone_name: str = "Asia/Shanghai"):
        self.crawler = crawler
        self.db = crawler.db
        self.timezone = ZoneInfo(timezone_name)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_checked_minute: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="grain-oil-crawl-scheduler", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("定时采集检查失败")
            self._stop_event.wait(30)

    def run_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        minute = current.replace(second=0, microsecond=0)
        if self._last_checked_minute == minute:
            return []
        self._last_checked_minute = minute
        results = []
        sources = self.db.fetch_all(
            'SELECT * FROM "CrsSource" WHERE "enabledFlag"=:enabled ORDER BY "crawlSourceId"',
            {"enabled": "启用"},
        )
        for source in sources:
            try:
                if not cron_matches(source["cronExpr"], current):
                    continue
                last_run_at = source.get("lastRunAt")
                if last_run_at:
                    last_run = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=timezone.utc)
                    elapsed = (current - last_run.astimezone(self.timezone)).total_seconds()
                    if elapsed < int(source["minIntervalMinutes"]) * 60:
                        continue
                result = self.crawler.trigger(source["crawlSourceId"], "scheduler")
                results.append(result)
            except Exception:
                logger.exception("定时采集执行失败：%s", source["crawlSourceId"])
        return results

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
