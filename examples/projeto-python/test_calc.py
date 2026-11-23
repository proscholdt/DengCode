from calc import media, soma


def test_soma() -> None:
    assert soma(2, 3) == 5


def test_media() -> None:
    assert media([1, 2, 3]) == 2
