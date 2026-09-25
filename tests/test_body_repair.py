"""Починка сохранённых тел: заменяем только доказуемо дефектное и только своим."""

from oiltech_digest.ingestion import body_repair
from tests.test_article_fetcher import FEED_HTML, OWN_BODY, OWN_TITLE, PINNED_BODY, PINNED_TITLE

# Так старое извлечение сохраняло тело: блок статьи вместе с шапкой и заголовком.
PINNED_STORED = f"14 сентября 2026, 15:24 2 мин 474 Источник: ИНТИ {PINNED_TITLE} {PINNED_BODY}"
OWN_STORED = f"14 сентября 2026, 16:01 2 мин 7956 Источник: ОДК {OWN_TITLE} {OWN_BODY}"


def _article(raw_text: str, title: str = OWN_TITLE) -> dict:
    return {"id": 1, "source_id": 36, "title": title, "url": "https://neftegaz.ru/news/1", "raw_text": raw_text}


def test_foreign_body_is_replaced_by_own_block():
    decision, new = body_repair.plan_repair(_article(PINNED_STORED), FEED_HTML)

    assert (decision.action, decision.defect) == ("replace", "foreign")
    assert "газотурбинный двигатель" in new and "Гидра" not in new


def test_own_body_is_left_alone_without_downloading():
    decision, new = body_repair.plan_repair(_article(OWN_STORED), None)

    assert (decision.action, decision.reason) == ("skip", "stored body looks own")
    assert new == ""


def test_own_body_without_header_is_not_reprocessed():
    # Своё тело, но без шапки (лид первым): по началу похоже на чужое — сверяем по тексту.
    decision, _ = body_repair.plan_repair(_article(OWN_BODY), FEED_HTML)

    assert (decision.action, decision.reason) == ("skip", "same article as stored")


def test_garbled_title_is_reported_not_guessed():
    garbled = "Ð¨ÐºÐ¾Ð»Ð° ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½Ð¸Ñ Ð¡ÐºÐ¾Ð»ÐºÐ¾Ð²Ð¾"

    decision, _ = body_repair.plan_repair(_article(garbled * 3, title=garbled), FEED_HTML)

    assert (decision.action, decision.defect, decision.reason) == ("skip", "mojibake", "title is mojibake")


def test_oversized_body_is_replaced_even_though_own_text_is_inside():
    sheet = OWN_STORED + ' var config = {"a": 1};' * 3000

    decision, new = body_repair.plan_repair(_article(sheet), FEED_HTML)

    assert (decision.action, decision.defect) == ("replace", "oversized")
    assert len(new) < 5000


def test_repair_writes_only_replacements_and_reports_ids(monkeypatch):
    written = []
    monkeypatch.setattr(
        body_repair.repository, "update_article_full_text",
        lambda article_id, raw_text, truncated, status, method, **kw: written.append((article_id, status, method)),
    )
    articles = [
        {**_article(PINNED_STORED), "id": 11},
        {**_article(OWN_STORED), "id": 12},
    ]
    fetched = []

    result = body_repair.repair_bodies(
        articles, apply=True, fetch_fn=lambda url: fetched.append(url) or FEED_HTML, pause_seconds=0,
    )

    assert result["replaced_ids"] == [11]
    assert written == [(11, "ok", "lxml")]
    assert len(fetched) == 1  # своё тело не качаем вовсе


def test_dry_run_does_not_write(monkeypatch):
    monkeypatch.setattr(
        body_repair.repository, "update_article_full_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("сухой прогон пишет в базу")),
    )

    result = body_repair.repair_bodies([_article(PINNED_STORED)], apply=False, fetch_fn=lambda url: FEED_HTML,
                                       pause_seconds=0)

    assert result["replaced"] == 1 and result["apply"] is False


def test_new_text_must_carry_most_title_words_not_just_generic_ones():
    # Общий страж (20% слов) пропускает чужую нефтегазовую новость по «росси»+«энерг».
    title = "Россия и Украина договорились не наносить удары по энергообъектам"
    page = f"""<html><body><article><h1>Другая новость</h1><p>{"Энергетики России обсуждают тарифы на тепло. " * 20}</p>
    </article></body></html>""".encode()

    decision, _ = body_repair.plan_repair(_article(PINNED_STORED, title=title), page)

    assert (decision.action, decision.reason) == ("skip", "new text does not match title")


def test_teaser_left_after_guard_rejected_foreign_block_gets_own_full_text():
    # 246 статей Neftegaz: дозагрузка вытащила чужой блок, страж отбил, осталась строка лида.
    teaser = "Первый опытный образец планируется изготовить в 2028 году."

    decision, new = body_repair.plan_repair(_article(teaser), FEED_HTML)

    assert (decision.action, decision.defect) == ("replace", "truncated")
    assert "газотурбинный двигатель нового поколения" in new


def test_truncated_is_not_replaced_by_text_that_is_not_longer():
    page = f"<html><body><article><h1>{OWN_TITLE}</h1><p>Коротко.</p></article></body></html>".encode()

    decision, _ = body_repair.plan_repair(_article("Тизер " + OWN_TITLE), page)

    assert (decision.action, decision.reason) == ("skip", "new text too short")
