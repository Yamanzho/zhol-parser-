from zhol.agents.scout.match import Identity, verdict_for, Candidate
from zhol.agents.scout.normalize import address_parts, addresses_compatible, addresses_conflict


def test_plain_address_keeps_house():
    assert address_parts("Улица Гоголя, 84а") == ("ул гоголя", "84а")
    assert address_parts("Улица Ескараева, 3") == ("ул ескараева", "3")


def test_trailing_postcode_is_not_the_house():
    assert address_parts("улица Гоголя 84а, Алматы 050000")[1] == "84а"
    assert address_parts("Улица Ескараева, 3, Алматы 050060")[1] == "3"
    assert address_parts("улица Ескараева 3, Алматы 050060")[1] == "3"


def test_google_postcode_address_matches_2gis():
    assert addresses_compatible("Улица Гоголя, 84а", "улица Гоголя 84а, Алматы 050000")
    assert not addresses_conflict("Улица Гоголя, 84а", "улица Гоголя 84а, Алматы 050000")
    assert addresses_compatible("Улица Ескараева, 3", "улица Ескараева 3, Алматы 050060")


def test_different_house_still_conflicts():
    assert addresses_conflict("Улица Гоголя, 84а", "улица Гоголя 90, Алматы 050000")


def test_verdicts_for_reference_cards():
    dance = Identity("Dance Studio 29", "Улица Гоголя, 84а", ["77074853110"])
    shown = Candidate("1", "1", None, "Dance Studio 29", "улица Гоголя 84а, Алматы 050000", ["77074853110"])
    assert verdict_for(dance, shown)[0] == "same"
    hero = Identity("Hero’s Journey", "Улица Ескараева, 3", [])
    card = Candidate("1", "1", None, "Hero’s Journey", "улица Ескараева 3, Алматы 050060", [])
    assert verdict_for(hero, card)[0] == "same"


def test_lone_six_digit_number_is_still_a_house():
    assert address_parts("улица Абая 123456") == ("ул абая", "123456")
