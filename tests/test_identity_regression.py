"""Identity rules stay conservative. The integration API does not add another write path."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zhol.agents.scout.browser_2gis import BrowserCollectionRefused
from zhol.agents.scout.match import Candidate, Identity, choose, match_status
from zhol.integration.live import create_job
from zhol.integration.schema import IntegrationError


def test_two_equal_matches_stay_ambiguous() -> None:
    identity = Identity("AOE Beauty", "улица Абая, 10", ["+7 701 000 00 11"])
    same = Candidate("1", "100011", "https://example/1", "AOE", "ул. Абая, 10", ["87010000011"])
    twin = Candidate("4", "100012", "https://example/4", "AOE Beauty", "ул. Абая, 10", ["+7 701 000 00 11"])
    decision = choose(identity, [same, twin])
    assert decision.verdict == "ambiguous"
    assert decision.candidate is None
    assert match_status("ambiguous") == "not_found"


def test_shared_phone_and_other_address_is_not_merged() -> None:
    identity = Identity("Montebello", "проспект Абая, 164/6", ["+7 776 111 25 85"])
    branch = Candidate("b", "2", "https://example/b", "Montebello", "улица Гоголя, 5", ["+7 776 111 25 85"])
    decision = choose(identity, [branch])
    assert decision.verdict == "ambiguous"
    assert decision.candidate is None


def test_a_clear_card_is_not_replaced_by_a_branch() -> None:
    """A second card with the same phone and another address must not become the chosen card."""
    identity = Identity("Montebello", "проспект Абая, 164/6", ["+7 776 111 25 85"])
    same = Candidate("a", "1", "https://example/a", "Montebello", "пр-т Абая, 164/6", ["+7 776 111 25 85"])
    branch = Candidate("b", "2", "https://example/b", "Montebello", "улица Гоголя, 5", ["+7 776 111 25 85"])
    decision = choose(identity, [branch, same])
    assert decision.verdict == "same"
    assert decision.candidate is not None
    assert decision.candidate.candidate_id == "a"


def test_create_job_enqueues_the_existing_batch_and_does_not_insert_companies(monkeypatch: pytest.MonkeyPatch) -> None:
    from zhol.integration import live

    def plans(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["platforms"] == ["2gis"]
        return [
            {
                "source_mode": "browser_2gis",
                "city": "Алматы",
                "category": "салон красоты",
                "platforms": ["2gis"],
                "limit": 2,
                "catalog": "browser_2gis",
            }
        ]

    def enqueue(session: MagicMock, plan: dict[str, object], idempotency_key: str | None = None) -> dict[str, object]:
        session.add.assert_not_called()
        assert plan["source_mode"] == "browser_2gis"
        assert idempotency_key == "client-key"
        return {"id": 9, "duplicate": False}

    monkeypatch.setattr(live, "build_browser_plans", plans)
    monkeypatch.setattr(live, "_enqueue_browser", enqueue)
    monkeypatch.setattr(
        live,
        "job_view",
        lambda _session, job_id: {
            "id": job_id,
            "status": "queued",
            "duplicate": False,
        },
    )
    session = MagicMock()
    session.scalar.return_value = None
    body, duplicate = create_job(
        session,
        {"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2, "idempotency_key": "client-key"},
    )
    assert duplicate is False
    assert body["id"] == 9
    session.add.assert_not_called()


def test_closed_platform_does_not_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    from zhol.integration import live

    def refuse(**_kwargs: object) -> list[dict[str, object]]:
        raise BrowserCollectionRefused("Сбор закрыт разрешением.")

    enqueue = MagicMock(side_effect=AssertionError("collector started"))
    monkeypatch.setattr(live, "build_yandex_plans", refuse)
    monkeypatch.setattr(live, "_enqueue_maps", enqueue)
    session = MagicMock()
    with pytest.raises(IntegrationError) as caught:
        create_job(session, {"platform": "yandex", "city": "Алматы", "query": "салон", "limit": 1, "idempotency_key": None})
    assert caught.value.status == 403
    enqueue.assert_not_called()
    session.add.assert_not_called()
