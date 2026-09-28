"""Подключение к PostgreSQL и инициализация схемы."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

import psycopg

from oiltech_digest.config import DATABASE_URL

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_READ_ONLY_OPTION = "-c default_transaction_read_only=on"


def get_connection() -> psycopg.Connection:
    """Новое подключение к БД. Вызывающий отвечает за закрытие (или используйте `with`)."""
    return psycopg.connect(DATABASE_URL)


@contextmanager
def read_only_connection():
    """Соединение только для чтения: каждый запрос — своя транзакция READ ONLY.

    Автокоммит — чтобы проба не держала транзакцию, пока качает страницы: открытая
    транзакция держит блокировку таблицы и задержала бы выкат схемы на всё это время.
    """
    conn = get_connection()
    try:
        conn.commit()  # тестовое подключение открывает транзакцию своим SET search_path
        conn.autocommit = True
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


@contextmanager
def read_only_process():
    """Внутри блока каждое НОВОЕ соединение процесса — только для чтения: запись отклонит база.

    Через PGOPTIONS: его читает libpq при каждом подключении, поэтому запрет ложится и на
    соединения, которые открывает код глубже (repository.article_exists, get_source,
    тематики предфильтра), а не только на то, что откроет сама проба. Переменная окружения —
    на весь процесс, поэтому блок — для отдельного процесса CLI, не для API с его потоками.

    Если DATABASE_URL сам задаёт options, PGOPTIONS молча не действует — поэтому режим
    проверяется сразу, и без него блок не начнётся.
    """
    previous = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"{previous} {_READ_ONLY_OPTION}" if previous else _READ_ONLY_OPTION
    try:
        with get_connection() as conn:
            mode = conn.execute("SHOW transaction_read_only").fetchone()[0]
        if mode != "on":
            raise RuntimeError("режим только для чтения не включился (options в DATABASE_URL?) — "
                               "ничего не запущено")
        yield
    finally:
        if previous is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = previous


def init_db() -> list[str]:
    """Выполнить schema.sql (идемпотентно). Возвращает список таблиц после создания."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        conn.execute(sql)  # schema.sql без параметров → допускается несколько команд
        conn.commit()
    return list_tables()


def list_tables() -> list[str]:
    """Список таблиц в схеме public."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        return [row[0] for row in cur.fetchall()]
