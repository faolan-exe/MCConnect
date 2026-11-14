"""Mojang's name lookup (database/minecraft.py, ADR 0011): only a clear "no such account" counts as unknown."""
import pytest
import requests

from database import minecraft


class Answer:
    def __init__(self, status):
        self.status_code = status


@pytest.mark.parametrize("status, expected", [(200, True), (204, False), (404, False), (400, False),
                                              (429, None), (500, None), (503, None)])
def test_name_exists_by_status(monkeypatch, status, expected):
    urls = []
    monkeypatch.setattr(minecraft.requests, "get", lambda url, timeout: urls.append(url) or Answer(status))
    assert minecraft.Minecraft().name_exists("MrSchief4120") is expected
    assert urls == ["https://api.mojang.com/users/profiles/minecraft/MrSchief4120"]


def test_name_exists_when_mojang_does_not_answer(monkeypatch):
    def down(url, timeout):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(minecraft.requests, "get", down)
    assert minecraft.Minecraft().name_exists("Notch") is None


def test_name_is_escaped_in_the_url(monkeypatch):
    urls = []
    monkeypatch.setattr(minecraft.requests, "get", lambda url, timeout: urls.append(url) or Answer(404))
    minecraft.Minecraft().name_exists("a/../b?c")
    assert urls == ["https://api.mojang.com/users/profiles/minecraft/a%2F..%2Fb%3Fc"]
