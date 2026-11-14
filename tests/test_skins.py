"""Skins, heads and body pictures from the own cache (web/skins.py, ADR 0010)."""
import base64
import io
import json
import os
import time

import pytest
import requests
from PIL import Image

from tests.conftest import OTHER_UUID, PLAYER_UUID
from tests.test_web import app, client, on, online_player  # noqa: F401 (fixtures)
from web import skins

REAL_GET = skins._get  # before conftest's offline_skins replaces it

UUID = PLAYER_UUID.replace("-", "")
FACE = (200, 30, 30, 255)
HAT = (30, 30, 200, 255)


def skin_png(size=(64, 64), hat=None, slim=False):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    for x in range(8, 16):
        for y in range(8, 16):
            image.putpixel((x, y), FACE)
    if hat:
        for x in range(40, 48):
            for y in range(8, 16):
                image.putpixel((x, y), hat)
    for x in range(20, 28):  # body
        for y in range(20, 32):
            image.putpixel((x, y), (10, 200, 10, 255))
    for x in range(40, 54 if slim else 56):  # right arm, all sides (slim arms leave x 54-55 empty)
        for y in range(20, 32):
            image.putpixel((x, y), (9, 9, 9, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def textures(url):
    value = base64.b64encode(json.dumps({"textures": {"SKIN": {"url": url}}}).encode()).decode()
    return json.dumps({"id": UUID, "properties": [{"name": "textures", "value": value}]}).encode()


class Web:
    """Fake internet: url -> body (None = error/timeout), counts the requests."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, url):
        self.calls.append(url)
        return self.answers.get(url)


MOJANG = skins.MOJANG_PROFILE.format(UUID)
TEXTURE = "https://textures.minecraft.net/texture/abc123"
MC_HEADS_SKIN = f"https://mc-heads.net/skin/{UUID}"


@pytest.fixture
def web(monkeypatch):
    def install(answers):
        fake = Web(answers)
        monkeypatch.setattr(skins, "_get", fake)
        return fake
    return install


def pixel(png, xy=(0, 0)):
    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)


# ------------------------------------------------------------------ sources

def test_skin_comes_from_mojang_first(web):
    fake = web({MOJANG: textures(TEXTURE), TEXTURE: skin_png()})
    assert skins.skin(PLAYER_UUID) is not None
    assert fake.calls == [MOJANG, TEXTURE]


def test_mc_heads_only_when_mojang_fails(web):
    fake = web({MC_HEADS_SKIN: skin_png()})
    assert skins.skin(UUID) is not None
    assert fake.calls == [MOJANG, MC_HEADS_SKIN]


def test_texture_url_of_another_host_is_not_followed(web):
    fake = web({MOJANG: textures("http://evil.example/texture/abc123")})
    assert skins.skin(UUID) is None
    assert not any("evil" in url for url in fake.calls)


@pytest.mark.parametrize("data", [b"<html>down</html>", b"", None])
def test_answers_that_are_no_skin_are_not_stored(web, data):
    web({MC_HEADS_SKIN: data})
    assert skins.skin(UUID) is None
    assert not os.path.exists(skins._path("skins", f"{UUID}.png"))


def test_a_png_of_the_wrong_size_is_no_skin(web):
    out = io.BytesIO()
    Image.new("RGBA", (300, 300)).save(out, "PNG")
    web({MC_HEADS_SKIN: out.getvalue()})
    assert skins.skin(UUID) is None


@pytest.mark.parametrize("value", ["None", "", "../../etc/passwd", "Notch", "4ebe5f6f" * 5])
def test_invalid_uuids_never_reach_the_internet(web, value):
    fake = web({})
    assert skins.skin(value) is None
    assert fake.calls == []


# ------------------------------------------------------------------ cache

def test_fresh_skin_is_not_fetched_again(web):
    fake = web({MC_HEADS_SKIN: skin_png()})
    skins.skin(UUID)
    skins.skin(UUID)
    assert fake.calls.count(MC_HEADS_SKIN) == 1


def test_old_skin_stays_in_use_when_the_sources_are_down(web):
    web({MC_HEADS_SKIN: skin_png()})
    first = skins.skin(UUID)
    path = skins._path("skins", f"{UUID}.png")
    old = time.time() - skins.FRESH - 1
    os.utime(path, (old, old))
    fake = web({})
    assert skins.skin(UUID) == first
    assert MC_HEADS_SKIN in fake.calls  # it was tried


def test_after_a_failure_the_next_try_waits(web):
    fake = web({})
    skins.skin(UUID)
    tries = len(fake.calls)
    skins.skin(UUID)
    assert len(fake.calls) == tries  # within RETRY_AFTER: no new request
    failed = skins._path("skins", f"{UUID}.png.failed")
    old = time.time() - skins.RETRY_AFTER - 1
    os.utime(failed, (old, old))
    web({MC_HEADS_SKIN: skin_png()})
    assert skins.skin(UUID) is not None
    assert not os.path.exists(failed)


def test_stale_file_is_refreshed_when_the_source_answers(web):
    web({MC_HEADS_SKIN: skin_png(hat=HAT)})
    skins.skin(UUID)
    path = skins._path("skins", f"{UUID}.png")
    old = time.time() - skins.FRESH - 1
    os.utime(path, (old, old))
    web({MC_HEADS_SKIN: skin_png()})
    assert pixel(skins.skin(UUID), (40, 8))[3] == 0  # the new skin without hat


# ------------------------------------------------------------------ heads and bodies

def test_head_is_the_face_scaled_up(web):
    web({MC_HEADS_SKIN: skin_png()})
    head = skins.head_image(UUID, 64)
    assert head.size == (64, 64) and head.getpixel((0, 0)) == FACE and head.getpixel((63, 63)) == FACE


def test_hat_layer_covers_the_face(web):
    web({MC_HEADS_SKIN: skin_png(hat=HAT)})
    assert skins.head_image(UUID, 32).getpixel((5, 5)) == HAT


def test_solid_hat_of_old_skins_is_ignored(web):
    """Old 64x32 skins often fill the hat with one color; the game does not show it either."""
    web({MC_HEADS_SKIN: skin_png(size=(64, 32), hat=HAT)})
    assert skins.head_image(UUID, 32).getpixel((5, 5)) == FACE


@pytest.mark.parametrize("asked, size", [(0, 8), (-5, 8), (8, 8), (512, 512), (100000, 512)])
def test_head_size_is_limited(asked, size):
    assert skins.head_image(UUID, asked).size == (size, size)


def test_without_skin_the_placeholder(web):
    web({})
    head = skins.head_image(UUID, 16)
    assert head.size == (16, 16) and head.getpixel((0, 15))[3] == 255


def test_flat_body_when_mc_heads_has_no_body(web):
    web({MC_HEADS_SKIN: skin_png()})
    body = Image.open(io.BytesIO(skins.body_png(UUID, 120)))
    assert body.width == 16 * (240 // 32) and body.height == 32 * (240 // 32)  # 4 + 8 + 4 px wide, 32 high


def test_flat_body_of_slim_skin_has_thin_arms(web):
    web({MC_HEADS_SKIN: skin_png(slim=True)})
    assert Image.open(io.BytesIO(skins.body_png(UUID, 120))).width == 14 * (240 // 32)


def test_body_of_mc_heads_is_cached(web):
    out = io.BytesIO()
    Image.new("RGBA", (120, 270), (1, 2, 3, 255)).save(out, "PNG")
    fake = web({MC_HEADS_SKIN: skin_png(), f"https://mc-heads.net/body/{UUID}/120": out.getvalue()})
    assert pixel(skins.body_png(UUID, 118)) == (1, 2, 3, 255)  # 118 -> the nearest size 120
    skins.body_png(UUID, 120)
    assert fake.calls.count(f"https://mc-heads.net/body/{UUID}/120") == 1


# ------------------------------------------------------------------ routes

def test_head_route_on_server_and_main_domain(client, server, web):
    web({MC_HEADS_SKIN: skin_png()})
    for host in ("testdomain", None):
        response = client.get(f"/mc/avatar/{PLAYER_UUID}/32", **on(host))
        assert response.status_code == 200 and response.mimetype == "image/png"
        assert response.headers["Cache-Control"] == f"public, max-age={6 * 3600}"
        assert pixel(response.data) == FACE


def test_placeholder_is_cached_only_briefly(client, server):
    response = client.get("/mc/avatar/None/32", **on("testdomain"))
    assert response.status_code == 200 and response.headers["Cache-Control"] == "public, max-age=300"


def test_head_route_answers_304_for_the_same_etag(client, server, web):
    web({MC_HEADS_SKIN: skin_png()})
    etag = client.get(f"/mc/avatar/{UUID}/32", **on("testdomain")).headers["ETag"]
    again = client.get(f"/mc/avatar/{UUID}/32", headers={"If-None-Match": etag}, **on("testdomain"))
    assert again.status_code == 304


def test_skin_route(client, server, web):
    assert client.get(f"/mc/skin/{OTHER_UUID}", **on("testdomain")).status_code == 404
    web({MC_HEADS_SKIN: skin_png()})
    response = client.get(f"/mc/skin/{UUID}", **on("testdomain"))
    assert response.status_code == 200 and Image.open(io.BytesIO(response.data)).size == (64, 64)


def test_body_route(client, server, web):
    web({MC_HEADS_SKIN: skin_png()})
    assert client.get(f"/mc/body/{UUID}/120", **on("testdomain")).mimetype == "image/png"


# ------------------------------------------------------------------ pages

def test_pages_load_nothing_from_other_sites(client, online_player):
    """No CDN and no mc-heads.net in the browser: the CSP only allows this server."""
    response = client.get("/spieler?player=_Tobias4444", **on("testdomain"))
    html = response.get_data(as_text=True)
    assert "mc-heads.net" not in html and "cdnjs" not in html and "googleapis" not in html and "jsdelivr" not in html
    assert "/mc/skin/" in html and "vendor/minerender/skin.min.js" in html
    csp = response.headers["Content-Security-Policy"]
    assert "https://cdn" not in csp and "googleapis" not in csp and "connect-src 'self';" in csp


def test_player_page_fills_the_stats_table_without_waiting_for_images(client, online_player):
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "window.onload =" not in html and "addEventListener(\"load\"" not in html


def test_vendored_libraries_are_served(client):
    for path in ("vendor/jquery/jquery.min.js", "vendor/three/three.min.js", "vendor/minerender/skin.min.js"):
        assert client.get(f"/static/{path}", **on(None)).status_code == 200


# ------------------------------------------------------------------ sites that are down

def test_offline_mode_uuids_are_not_asked_at_mojang(web):
    offline = "4898da867b51379aad8bfafd8ce9ae1f"  # version 3 (name based)
    fake = web({})
    skins.skin(offline)
    assert fake.calls == [f"https://mc-heads.net/skin/{offline}"]


def test_a_host_that_does_not_answer_is_skipped_for_a_while(monkeypatch):
    calls = []

    def hanging(url, **kwargs):
        calls.append(url)
        raise requests.Timeout()
    monkeypatch.setattr(skins, "_get", REAL_GET)
    monkeypatch.setattr(skins.requests, "get", hanging)
    monkeypatch.setattr(skins, "_down_until", {})
    assert skins._get("https://mc-heads.net/skin/a") is None
    assert skins._get("https://mc-heads.net/skin/b") is None
    assert calls == ["https://mc-heads.net/skin/a"]
    assert skins._get("https://sessionserver.mojang.com/x") is None  # other hosts are still asked
    assert len(calls) == 2
    monkeypatch.setattr(skins, "_down_until", {"mc-heads.net": 0})
    skins._get("https://mc-heads.net/skin/c")
    assert len(calls) == 3


def test_minerender_does_not_report_page_views(client, online_player):
    """MineRender posts every page view to minerender.org/stats.php unless sendStats is off (the CSP blocked it and
    the console showed the error)."""
    html = client.get("/spieler?player=_Tobias4444", **on("testdomain")).get_data(as_text=True)
    assert "sendStats: false," in html
