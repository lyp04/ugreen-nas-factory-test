from types import SimpleNamespace

from src.gui import LANGUAGE_OPTIONS, UI_TEXT, FactoryTestGUI


def _gui_with_language(label: str):
    gui = FactoryTestGUI.__new__(FactoryTestGUI)
    gui.language_var = SimpleNamespace(get=lambda: label)
    return gui


def test_all_languages_have_the_same_ui_text_keys() -> None:
    expected = set(UI_TEXT["zh"])

    assert "Español (México)" in LANGUAGE_OPTIONS
    assert set(UI_TEXT["en"]) == expected
    assert set(UI_TEXT["es-MX"]) == expected


def test_mexican_spanish_language_translates_core_ui_text_and_status() -> None:
    gui = _gui_with_language("Español (México)")

    assert gui._language_code() == "es-MX"
    assert gui._t("ready") == "Listo"
    assert gui._t("queue_summary", total=2, active=1) == "Dispositivos conectados: 2, activos 1"
    assert gui._status_text("running") == "En curso"


def test_unknown_language_falls_back_to_chinese() -> None:
    gui = _gui_with_language("unknown")

    assert gui._language_code() == "zh"
    assert gui._t("ready") == "就绪"
