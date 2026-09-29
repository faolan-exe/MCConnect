"""Tooltips in chat lines (database/chat.py, plugin 3.20 "hover")."""
from database import achievements, chat, metrics, motivation
from mc_socket import commands
from tests.conftest import PLAYER_UUID
from tests.test_socket import plugin, socket_server  # noqa: F401 (fixtures)


def test_hover_markup():
    text = chat.hover("»Titel«", "&7Zeile 1", None, "&fZeile ⟦2⟧|~")
    assert text == "⟦»Titel«⇝&7Zeile 1¶&fZeile 2⟧"
    assert chat.without_hover(f"a {text} b") == "a »Titel« b"
    assert chat.hover("x") == "x"  # no lines: no tooltip
    assert commands.clean("⟦böse⇝tooltip⟧") == "bösetooltip"  # players cannot write tooltips


def test_announcements_have_short_details():
    miner = achievements.ACHIEVEMENTS_BY_KEY["miner"]
    _, text = achievements.announcement("Steve", miner, 0)
    assert chat.without_hover(text) == "★ Steve hat den Erfolg »Bergarbeiter« (Bronze) erreicht!"
    assert "&7Bronze: &f10.000¶&7Nächste Stufe Silber: &f100.000" in text
    assert "Die höchste Stufe!" in achievements.announcement("Steve", miner, 3)[1]
    metric = metrics.METRICS_BY_KEY["blocks_mined"]
    _, record = motivation.record_announcement("Steve", metric, "Alex", 500, 107)
    assert "&7Vorher: &fAlex &7– &f107" in record and chat.without_hover(record).startswith("★ Neuer Rekord! Steve hat Alex")
    assert "&7Nächstes: &f30 Tage" in motivation.milestone_announcement("Steve", "streak", 7)[1]
    assert text.count("¶") <= 3 and record.count("¶") <= 3  # short, no novel


def test_old_plugins_get_the_label_only(server, plugin, socket_server):
    old = plugin().auth(server["key"])
    old.send("!FEATURES~click")
    assert old.recv() == "success|106"
    socket_server.tell(server["id"], PLAYER_UUID, "Hallo " + chat.hover("Welt", "&7Tooltip"))
    assert old.recv() == f"!tell~{PLAYER_UUID}|Hallo Welt"


def test_new_plugins_get_the_tooltip(server, plugin, socket_server):
    new = plugin().auth(server["key"])
    new.send("!FEATURES~click,hover")
    assert new.recv() == "success|106"
    socket_server.tell(server["id"], PLAYER_UUID, "Hallo " + chat.hover("Welt", "&7Tooltip"))
    assert new.recv() == f"!tell~{PLAYER_UUID}|Hallo ⟦Welt⇝&7Tooltip⟧"
