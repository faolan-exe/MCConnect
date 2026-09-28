"""MCConnect web server.

Main domain (SERVER_NAME, e.g. mc.tobisit.de): landing page, server admin signup/login.
Server pages (<subdomain>.SERVER_NAME): player list, player stats, player login.

Run for development:  python web/main.py
Run in production:    gunicorn 'web.main:create_app()'
"""
import functools
import glob
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlparse

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psycopg2.errors
from colorlogx import get_logger
from flask import (Blueprint, Flask, Response, abort, current_app, g, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from database import achievements as achievements_mod
from database import config
from database import metrics as metrics_mod
from database import motivation
from mc_socket import commands as game_commands
from database.databaseManagerV2 import MAX_GALLERY_IMAGES, MODERATOR_LEVEL, DatabaseManager
from database import stats as stats_mod
from database.stats import format_time
from web.uploads import FILENAME_RE, MAX_UPLOAD_BYTES, InvalidImage, delete_images, save_image

logger = get_logger("webServer")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])$")
RESERVED_SUBDOMAINS = {"www", "mc", "api", "admin", "app", "mail", "smtp", "static", "plugin", "connect",
                       "socket", "traefik", "dashboard", "status", "help", "support", "login", "manage"}
# Minecraft chat colors that can be used for prefixes, with their web color.
PREFIX_COLORS = {
    "black": "#000000", "dark_blue": "#0000AA", "dark_green": "#00AA00", "dark_aqua": "#00AAAA",
    "dark_red": "#AA0000", "dark_purple": "#AA00AA", "gold": "#FFAA00", "gray": "#AAAAAA",
    "dark_gray": "#555555", "blue": "#5555FF", "green": "#55FF55", "aqua": "#55FFFF",
    "red": "#FF5555", "light_purple": "#FF55FF", "yellow": "#FFFF55", "white": "#FFFFFF",
}
# Colors only moderators may use for their own prefix.
MODERATOR_PREFIX_COLORS = {"gold"}
# No formatting characters (§ &), no protocol separators (~ |), no % (chat format).
PREFIX_TEXT_RE = re.compile(r"^[A-Za-z0-9ÄÖÜäöüß _.!?+*#-]{1,16}$")
# The live values (players online, status, player cards) are polled by the pages (web/static/poll.js);
# one result is shared by all requests for this long, so many open tabs cost one query.
LIVE_CACHE_SECONDS = 2
# Time ranges of the rankings and the comparison (?zeitraum=...): days, None = all time.
PERIODS = {"gesamt": None, "30": 30, "7": 7}
MAX_COMPARED_PLAYERS = 4
# Categorical colors of the compared players (in this order, validated for color blindness).
COMPARE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
HISTORY_DAYS = 90

main_bp = Blueprint("main", __name__)
server_bp = Blueprint("server", __name__, subdomain="<subdomain>")


def db():
    return current_app.extensions["mcconnect_db"]


class LiveCache:
    """Results of the live endpoints, shared for LIVE_CACHE_SECONDS (per worker process)."""

    def __init__(self, seconds=LIVE_CACHE_SECONDS):
        self.seconds = seconds
        self._values = {}
        self._lock = threading.Lock()

    def get(self, key, compute):
        now = time.monotonic()
        with self._lock:
            hit = self._values.get(key)
        if hit and now - hit[0] < self.seconds:
            return hit[1]
        value = compute()
        with self._lock:
            if len(self._values) > 5_000:
                self._values = {k: v for k, v in self._values.items() if now - v[0] < self.seconds}
            self._values[key] = (now, value)
        return value


def live_json(key, compute):
    """A polled live value as JSON (cached briefly, never stored by the browser or a proxy)."""
    response = current_app.response_class(json.dumps(current_app.extensions["mcconnect_live"].get(key, compute)),
                                          mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def safe_next_path(path):
    """
    Only allow local absolute paths as redirect target. Whitespace and control characters are refused:
    browsers drop tabs and newlines from URLs, so "/\\t/evil.example" would turn into "//evil.example".
    """
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path \
            or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in path):
        return "/"
    return path


class RateLimiter:
    """At most `limit` hits per key within `window` seconds (in memory, per worker process)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        """Count a hit; False if the key already reached the limit."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            self._hits[key] = hits + [now]
            if len(self._hits) > 10_000:  # forget old clients
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
            return True


def rate_limit(name, key):
    """Count a hit of the named limit (see create_app) for the key; False if it is exhausted."""
    return current_app.extensions["mcconnect_limits"][name].allow(key)


################################ SERVER PAGES (subdomain) #################################

@server_bp.url_value_preprocessor
def load_server(endpoint, values):
    g.subdomain = values.pop("subdomain").lower()
    g.server = db().get_server_information_dict(g.subdomain)
    if g.server is None:
        abort(404)


def logged_in_player_id():
    """The player_id of the logged in player on the current server, or None."""
    if session.get("server_id") != g.server["id"]:
        return None
    return session.get("player_id")


@server_bp.context_processor
def inject_server_context():
    player_id = logged_in_player_id()
    name = ""
    permission_level = 99
    if player_id:
        name = db().get_player_name_from_player_id(player_id) or ""
        permission_level = db().get_web_access_permission_from_player_id(player_id)
        if db().is_moderator(player_id):  # includes OPs when the server allows it
            permission_level = min(permission_level or 99, MODERATOR_LEVEL)
    server = g.server
    settings = db().get_server_settings(server["id"])
    # loginVar is only rendered while logged out (name == "").
    return dict(loginVar="<a href=\"/login\" id=loginLink>Login</a>",
                prefix_colors=PREFIX_COLORS,
                perm=permission_level if permission_level is not None else 99,
                uuid_profile=session.get("uuid") if player_id else None,
                name=name,
                server_description_short=server["server_description_short"],
                server_description_long=server["server_description_long"],
                discord_url=server["discord_url"],
                license_type=server["license_type"],
                mc_server_domain=server["mc_server_domain"],
                whitelist=server["whitelist"],
                rules_enabled=settings["rules_enabled"], access_open=settings["access_mode"] != "off",
                server_name=server["server_name"])


@server_bp.route("/")
def subdomain_index_route():
    images = db().get_server_images(g.server["id"])
    records, recap, competitions, goals = [], None, [], []
    events = [event_view(e) for e in db().list_events(g.server["id"], limit=2)]
    polls = [poll_view(p) for p in db().list_polls(g.server["id"], open_only=True, limit=2)]
    if current_app.config["FEATURE_RANKINGS"]:
        players = list(visible_players(g.server["id"]).values())
        recap = weekly_recap(g.server["id"], players)
        today = db().get_today()
        competitions = [v for v in (competition_view(c, today, limit=3) for c in db().list_competitions(g.server["id"])
                                    if c["starts_on"] <= today <= c["ends_on"]) if v]
        goals = [v for v in (goal_view(goal, today) for goal in db().list_goals(g.server["id"]))
                 if v and v["status"] in ("running", "reached_recently")]
        held = db().get_current_records(g.server["id"])
        for key in metrics_mod.RECORD_METRICS:
            metric = metrics_mod.METRICS_BY_KEY[key]
            best = max(players, key=lambda p: p["values"][key], default=None)
            if best and best["values"][key] > 0:
                record = held.get(key)
                since = record["since"] if record and record["name"] == best["name"] and not record["first"] else None
                records.append({"label": metric.label, "name": best["name"], "uuid": best["uuid"],
                                "text": metrics_mod.format_value(metric, best["values"][key]),
                                "since": since.strftime("%d.%m.%Y") if since else None})
    return render_template("index-subpage.html",
                           player_total=len(db().get_all_player_ids_from_subdomain(g.subdomain)),
                           records=records, recap=recap, competitions=competitions, goals=goals,
                           events=events, polls=polls,
                           banner_url=image_url(images["banner"]) if images["banner"] else None,
                           gallery_urls=[image_url(image["filename"]) for image in images["gallery"]])


@server_bp.route("/login", methods=["GET", "POST"])
def player_login():
    if request.method == "POST":
        if request.form.get("text_input") != "logout":
            abort(400)
        session.clear()
        return redirect("/")

    if logged_in_player_id():
        return render_template("logout_confirmation.html", uuid=session.get("uuid"))
    next_path = request.args.get("next")
    if next_path is None or safe_next_path(next_path) != next_path:
        referrer_path = safe_next_path(urlparse(request.referrer).path) if request.referrer else "/"
        return redirect(f"/login?next={quote(referrer_path, safe='/')}")
    pending_name = session.get("login_name") if session.get("login_server_id") == g.server["id"] else None
    return render_template("login.html", uuid=pending_name or "")


@server_bp.route("/spieler")
def player_overview_route():
    """Player list, or the stats of one player with ?player=<name>."""
    user_name = request.args.get("player")
    if not user_name:
        return player_list_page()

    player_id = db().get_player_id_from_player_name_and_server_id(user_name, g.server["id"])
    if player_id is None:
        abort(404)
    info = db().get_player_info_by_player_id(player_id)
    viewer = logged_in_player_id()
    is_self = viewer is not None and str(viewer) == str(player_id)
    if info["hide_stats"] and not is_self:
        return render_template("spieler_verborgen.html", player_name=info["name"], uuid=info["mojang_uuid"],
                               bio=info["bio"], player_prefix=db().get_player_prefix(player_id))

    startdate, enddate = "", ""
    banned = db().get_ban_reason_from_player_id(player_id)
    if banned:
        start, end = db().get_ban_start_and_ban_end_by_player_id(player_id)
        startdate = start.strftime("%d.%m.%Y %H:%M")
        enddate = end.strftime("%d.%m.%Y %H:%M") if end else "dauerhaft"

    return render_template(
        "spieler-info.html", uuid=info["mojang_uuid"], user_name=info["name"], status=info["online"],
        extras=player_extras(str(player_id)), bio=info["bio"], is_self=is_self, hidden=info["hide_stats"],
        notes=[dict(n, when=n["created_at"].strftime("%d.%m.%Y, %H:%M")) for n in db().get_player_notes(player_id)]
        if viewer and db().is_moderator(viewer) else None,
        player_log=mod_log_view(g.server["id"], info["name"], 20) if viewer and db().is_moderator(viewer) else None,
        is_favorite=bool(viewer) and not is_self and info["name"] in {f["name"] for f in db().get_favorites(viewer)},
        logged_in=bool(viewer), card_url=url_for("server.player_card", subdomain=g.subdomain, player_name=info["name"], _external=True),
        player_prefix=db().get_player_prefix(player_id),
        guestbook=[dict(e, when=e["created_at"].strftime("%d.%m.%Y, %H:%M"),
                        can_delete=bool(viewer) and (str(viewer) in (e["author_id"], e["player_id"]) or db().is_moderator(viewer)))
                   for e in db().get_guestbook(player_id)],
        builds=[build_view(b) for b in db().list_builds(g.server["id"], player_id=player_id, limit=6)],
        guestbook_max=GUESTBOOK_MAX_LENGTH,
        warnings=[dict(w, when=w["created_at"].strftime("%d.%m.%Y, %H:%M")) for w in db().get_warnings(player_id)]
        if viewer and db().is_moderator(viewer) else None,
        muted_until=info["muted_until"].strftime("%d.%m. %H:%M") if info.get("muted_until") and
        info["muted_until"] > datetime.now(timezone.utc) else None,
        banned=bool(banned), startdate=startdate, enddate=enddate,
        armor_stats=json.dumps(db().get_all_armor_stats(player_id)),
        tool_stats=json.dumps(db().get_all_tools_stats(player_id)),
        item_stats=json.dumps(db().get_all_items_stats(player_id)),
        block_stats=json.dumps(db().get_all_blocks_stats(player_id)),
        mob_stats=json.dumps(db().get_all_mobs_stats(player_id)),
        custom_stats=json.dumps(db().get_all_custom_stats(player_id)))


def feature_rankings_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config["FEATURE_RANKINGS"]:
            abort(404)
        return view(*args, **kwargs)
    return wrapper


def player_required(view=None, *, allow_upload=False):
    """
    Server pages that need a logged in player; POST API calls must be JSON (CSRF protection).
    Upload endpoints (allow_upload) accept multipart forms instead and check the Origin header.
    """
    if view is None:
        return functools.partial(player_required, allow_upload=allow_upload)

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        is_api = request.path.startswith("/api/")
        if not logged_in_player_id():
            return ({"error": "Bitte zuerst einloggen."}, 401) if is_api else redirect(f"/login?next={quote(request.path, safe='/')}")
        if is_api and request.method == "POST":
            if allow_upload and request.mimetype == "multipart/form-data":
                origin = request.headers.get("Origin")
                if origin and urlparse(origin).netloc != request.host:
                    return {"error": "cross-site upload refused"}, 403
            elif not request.is_json:
                return {"error": "json required"}, 415
        return view(*args, **kwargs)
    return wrapper


def moderator_required(view):
    @functools.wraps(view)
    @player_required
    def wrapper(*args, **kwargs):
        if not db().is_moderator(logged_in_player_id()):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def selected_period():
    period = request.args.get("zeitraum", "gesamt")
    return period if period in PERIODS else "gesamt"


def period_hint(server_id, days):
    """A note if the snapshots do not reach back to the start of the time range yet."""
    if days is None:
        return None
    start = db().get_snapshot_start(server_id)
    if start is None:
        return "Zeiträume werden ab jetzt aufgezeichnet. Bis dahin gibt es hier noch keine Werte."
    if start > date.today() - timedelta(days=days):
        return (f"Zeiträume werden erst seit dem {start.strftime('%d.%m.%Y')} aufgezeichnet, "
                "davor fehlen die Werte noch.")
    return None


def ranked(players, metric):
    """Players with a value for the metric, best first, with competition ranking (1, 1, 3)."""
    rows = sorted((p for p in players if p["values"][metric.key] > 0),
                  key=lambda p: (-p["values"][metric.key], p["name"].lower()))
    top = rows[0]["values"][metric.key] if rows else 0
    result = []
    for index, player in enumerate(rows):
        value = player["values"][metric.key]
        rank = result[-1]["rank"] if result and result[-1]["value"] == value else index + 1
        result.append({"rank": rank, "name": player["name"], "uuid": player["uuid"], "value": value,
                       "text": metrics_mod.format_value(metric, value), "share": value / top})
    return result


@server_bp.route("/rangliste")
@feature_rankings_required
def rankings_page():
    period = selected_period()
    players = db().get_server_metrics(g.server["id"], PERIODS[period])
    groups = [(label, [(metric, ranked(players, metric)) for metric in metrics_mod.METRICS if metric.group == key])
              for key, label in metrics_mod.GROUPS]
    return render_template("rangliste.html", groups=groups, period=period, periods=PERIODS,
                           hint=period_hint(g.server["id"], PERIODS[period]),
                           prefixes=db().get_all_worn_prefixes(g.server["id"]))


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez."]
# Sequential green ramp for the peak time heatmap (light = few players, dark = many).
HEATMAP_RAMP = ["#e3f5e9", "#bde6cb", "#8fd4a8", "#5fc084", "#35a862", "#1f8f47", "#146b34"]
RECAP_METRICS = (("play_time", "Spieler der Woche", "am längsten online"), ("blocks_mined", "Baumeister", "Blöcke abgebaut"),
                 ("mob_kills", "Monsterjäger", "Mobs getötet"), ("diamonds", "Diamantenfieber", "Diamanterz abgebaut"))


def weekly_recap(server_id, players):
    """The best players of the last full week (Monday to Sunday), or None without complete data."""
    today = db().get_today()
    end = today - timedelta(days=today.weekday() + 1)  # last Sunday
    start = end - timedelta(days=6)
    first = db().get_snapshot_start(server_id)
    if first is None or first >= start:
        return None  # the baseline before the week is missing
    gains = db().get_metrics_between(server_id, start, end)
    by_id = {p["player_id"]: p for p in players}
    tiles = []
    for key, title, what in RECAP_METRICS:
        metric = metrics_mod.METRICS_BY_KEY[key]
        best = max(((pid, values.get(key, 0)) for pid, values in gains.items() if pid in by_id),
                   key=lambda item: (item[1], by_id[item[0]]["name"].lower()), default=None)
        if best and best[1] > 0:
            player = by_id[best[0]]
            tiles.append({"title": title, "name": player["name"], "uuid": player["uuid"],
                          "text": f"{metrics_mod.format_value(metric, best[1])} {what}"})
    if not tiles:
        return None
    play_time = sum(values.get("play_time", 0) for values in gains.values())
    return {"week": start.isocalendar()[1], "start": start.strftime("%d.%m."), "end": end.strftime("%d.%m.%Y"),
            "tiles": tiles, "active": db().get_active_players_between(server_id, start, end),
            "new": db().get_new_players_between(server_id, start, end),
            "play_time": format_time(play_time / 20)}


@server_bp.route("/server-statistik")
@feature_rankings_required
def server_stats_page():
    server_id = g.server["id"]
    players = db().get_server_metrics(server_id, include_hidden=True)  # sums count everyone
    today = db().get_today()
    totals = {m.key: sum(p["values"][m.key] for p in players) for m in metrics_mod.METRICS}
    groups = [(label, [(m, metrics_mod.format_value(m, totals[m.key])) for m in metrics_mod.METRICS if m.group == key])
              for key, label in metrics_mod.GROUPS]
    peak, peak_at = db().get_online_peak(server_id)

    fmt_time = lambda ts: ts.strftime("%d.%m. %H:%M")
    online = {
        "day": [{"t": fmt_time(t), "v": n} for t, n in db().get_online_history(server_id, 24, 15)],
        "week": [{"t": f"{WEEKDAYS[t.weekday()]} {fmt_time(t)}", "v": n}
                 for t, n in db().get_online_history(server_id, 24 * 7, 60)],
    }
    matrix = db().get_peak_times(server_id)
    top_hour = max(max(row) for row in matrix)
    heatmap = [{"day": WEEKDAYS[d], "cells": [{
        "hour": h, "value": v,
        "level": min(len(HEATMAP_RAMP) - 1, int(v / top_hour * len(HEATMAP_RAMP))) if v and top_hour else None,
        "color": HEATMAP_RAMP[min(len(HEATMAP_RAMP) - 1, int(v / top_hour * len(HEATMAP_RAMP)))] if v and top_hour else None,
        "tip": f"{WEEKDAYS[d]} {h}–{h + 1} Uhr: Ø {v:.1f} Spieler".replace(".", ","),
    } for h, v in enumerate(row)]} for d, row in enumerate(matrix)]

    daily = [{"label": d.strftime("%d.%m."), "tip": d.strftime("%d.%m.%Y"),
              "value": None if v is None else round(v / 20 / 3600, 2),
              "text": "keine Daten" if v is None else format_time(v / 20)}
             for d, v in db().get_server_daily_gain(server_id, "play_time", 30)]
    all_ids = [p["player_id"] for p in players]
    top = lambda category: next(((obj, sum(v.values())) for obj, v in db().get_top_objects(all_ids, category, 1)), None) \
        if all_ids else None
    facts = motivation.fun_facts(totals, len(players), top_block=top(stats_mod.BLOCK_MINED),
                                 top_killer=top(stats_mod.MOB_KILLED_BY), top_mob=top(stats_mod.MOB_KILLED))[:8]
    months = [{"label": MONTHS[m.month - 1], "tip": f"{MONTHS[m.month - 1]} {m.year}", "value": n,
               "text": f"{n} neue Spieler" if n != 1 else "1 neuer Spieler"}
              for m, n in db().get_new_players_per_month(server_id)]
    return render_template(
        "server_statistik.html", groups=groups, player_count=len(players),
        online_now=sum(p["online"] for p in players),
        new_players=db().get_new_players_between(server_id, today - timedelta(days=29), today),
        play_time_total=metrics_mod.format_value(metrics_mod.METRICS_BY_KEY["play_time"], totals["play_time"]),
        peak=peak, peak_at=peak_at.strftime("%d.%m.%Y, %H:%M Uhr") if peak_at else None,
        online=online, heatmap=heatmap, heatmap_ramp=HEATMAP_RAMP, heatmap_top=top_hour, facts=facts,
        daily=daily if any(d["value"] is not None for d in daily) else None, months=months)


@server_bp.route("/teams")
@feature_rankings_required
def teams_page():
    """Prefixes as teams: sum of a metric over all members."""
    period = selected_period()
    metric = metrics_mod.METRICS_BY_KEY.get(request.args.get("kennzahl"), metrics_mod.METRICS_BY_KEY["play_time"])
    players = db().get_server_metrics(g.server["id"], PERIODS[period])
    prefixes = db().get_all_worn_prefixes(g.server["id"])
    teams = {}
    for p in players:
        prefix = prefixes.get(p["uuid"])
        if prefix:
            team = teams.setdefault(prefix[0].lower(), {"text": prefix[0], "color": prefix[1], "members": [], "value": 0})
            team["members"].append(p)
            team["value"] += p["values"][metric.key]
    ranked_teams = sorted(teams.values(), key=lambda t: (-t["value"], t["text"].lower()))
    top = ranked_teams[0]["value"] if ranked_teams else 0
    for index, team in enumerate(ranked_teams):
        team["rank"] = index + 1
        team["share"] = team["value"] / top if top else 0
        team["text_value"] = metrics_mod.format_value(metric, team["value"])
        team["average"] = metrics_mod.format_value(metric, team["value"] // len(team["members"]))
        team["members"].sort(key=lambda p: -p["values"][metric.key])
    extra = f"&kennzahl={metric.key}"
    return render_template("teams.html", teams=ranked_teams, metric=metric, metrics=metrics_mod.METRICS,
                           groups=metrics_mod.GROUPS, period=period, periods=PERIODS, extra=extra,
                           hint=period_hint(g.server["id"], PERIODS[period]))


def competition_view(competition, today, limit=None):
    metric = metrics_mod.METRICS_BY_KEY.get(competition["metric"])
    if metric is None:
        return None
    status = ("upcoming" if competition["starts_on"] > today else
              "finished" if competition["ends_on"] < today else "running")
    standings = db().get_competition_standings(competition) if status != "upcoming" else []
    top = standings[0]["value"] if standings else 0
    rows = [{"rank": i + 1, "name": r["name"], "uuid": r["uuid"], "text": metrics_mod.format_value(metric, r["value"]),
             "share": r["value"] / top if top else 0} for i, r in enumerate(standings[:limit] if limit else standings)]
    days_left = (competition["ends_on"] - today).days + 1
    return dict(competition, metric_label=metric.label, status=status, rows=rows,
                starts=competition["starts_on"].strftime("%d.%m.%Y"), ends=competition["ends_on"].strftime("%d.%m.%Y"),
                days_left=days_left, days_until=(competition["starts_on"] - today).days)


@server_bp.route("/wettbewerbe")
@feature_rankings_required
def competitions_page():
    today = db().get_today()
    views = [v for v in (competition_view(c, today) for c in db().list_competitions(g.server["id"])) if v]
    goals = [v for v in (goal_view(goal, today) for goal in db().list_goals(g.server["id"])) if v]
    return render_template("wettbewerbe.html", goals=goals,
                           running=[v for v in views if v["status"] == "running"],
                           upcoming=sorted((v for v in views if v["status"] == "upcoming"), key=lambda v: v["starts_on"]),
                           finished=[v for v in views if v["status"] == "finished"])


def since_text(when, today):
    """"seit 3 Tagen", "seit 2 Jahren" ..."""
    days = (today - when).days
    if days >= 365:
        years = days // 365
        return "seit 1 Jahr" if years == 1 else f"seit {years} Jahren"
    if days >= 60:
        return f"seit {days // 30} Monaten"
    return "seit heute" if days == 0 else "seit 1 Tag" if days == 1 else f"seit {days} Tagen"


def streak_rows(streaks, names, key, limit=10):
    rows = sorted(((pid, s) for pid, s in streaks.items() if s[key] > 0 and pid in names),
                  key=lambda item: (-item[1][key], names[item[0]]["name"].lower()))[:limit]
    return [{"name": names[pid]["name"], "uuid": names[pid]["uuid"], "days": s[key],
             "range": f"{s['best_first'].strftime('%d.%m.%Y')} – {s['best_last'].strftime('%d.%m.%Y')}" if key == "best" else None}
            for pid, s in rows]


@server_bp.route("/ruhmeshalle")
@feature_rankings_required
def hall_of_fame_page():
    server_id = g.server["id"]
    today = db().get_today()
    names = visible_players(server_id)
    held = db().get_current_records(server_id)
    records = []
    for metric in motivation.RECORD_METRICS:
        record = held.get(metric.key)
        if record:
            records.append({"label": metric.label, "name": record["name"], "uuid": record["uuid"],
                            "text": metrics_mod.format_value(metric, record["value"]),
                            "since": None if record["first"] else record["since"].strftime("%d.%m.%Y")})
    history = [dict(h, label=metrics_mod.METRICS_BY_KEY[h["metric"]].label, when=h["at"].strftime("%d.%m.%Y, %H:%M"),
                    text=metrics_mod.format_value(metrics_mod.METRICS_BY_KEY[h["metric"]], h["value"]))
               for h in db().get_record_history(server_id, 25) if h["metric"] in metrics_mod.METRICS_BY_KEY]
    winners = {}
    for t in db().get_server_trophies(server_id, "competition", 60):
        entry = winners.setdefault(t["ref"], {"title": t["title"], "date": t["awarded_at"].strftime("%d.%m.%Y"), "places": []})
        entry["places"].append(dict(t, color=motivation.PLACE_COLORS.get(t["place"])))
    for entry in winners.values():
        entry["places"].sort(key=lambda t: t["place"])
    weekly = db().get_server_trophies(server_id, "player_of_week", 12)
    week_counts = {}
    for t in db().get_server_trophies(server_id, "player_of_week", 1000):
        week_counts[t["name"]] = week_counts.get(t["name"], 0) + 1
    streaks = db().get_streaks(server_id)
    goals = [v for v in (goal_view(goal, today) for goal in db().list_goals(server_id)) if v and v["done"]]
    return render_template(
        "ruhmeshalle.html", records=records, history=history, winners=list(winners.values()),
        weekly=[dict(t, total=week_counts.get(t["name"], 1)) for t in weekly],
        best_streaks=streak_rows(streaks, names, "best"), current_streaks=streak_rows(streaks, names, "current"),
        milestones=motivation.STREAK_MILESTONES,
        veterans=[dict(v, since=since_text(v["first_seen"].date(), today), date=v["first_seen"].strftime("%d.%m.%Y"))
                  for v in db().get_veterans(server_id)],
        collectors=db().get_achievement_leaders(server_id),
        achievement_total=len(achievements_mod.ACHIEVEMENTS) * len(achievements_mod.TIERS), goals=goals)


GOAL_RECENT_DAYS = 7


def visible_players(server_id):
    """{player_id: player} of get_server_metrics (visible players, all time), cached for the request."""
    cache = g.setdefault("visible_players", {})
    if server_id not in cache:
        cache[server_id] = {p["player_id"]: p for p in db().get_server_metrics(server_id)}
    return cache[server_id]


def goal_view(goal, today, contributors=3):
    """Display data of a community goal: progress, status and the players who contributed most."""
    metric = metrics_mod.METRICS_BY_KEY.get(goal["metric"])
    if metric is None:
        return None
    total, per_player = db().get_goal_progress(goal, today)
    done = goal["reached_at"] is not None or total >= goal["target"]
    if goal["starts_on"] > today:
        status = "upcoming"
    elif done:
        reached = goal["reached_at"].date() if goal["reached_at"] else today
        status = "reached_recently" if (today - reached).days < GOAL_RECENT_DAYS else "reached"
    elif goal["ends_on"] and goal["ends_on"] < today:
        status = "failed"
    else:
        status = "running"
    visible = visible_players(goal["server_id"])
    top = sorted(((pid, v) for pid, v in per_player.items() if v > 0 and pid in visible),
                 key=lambda item: (-item[1], visible[item[0]]["name"].lower()))[:contributors]
    return dict(goal, metric_label=metric.label, status=status, done=done,
                share=min(1.0, total / goal["target"]), percent=min(100, int(total * 100 // goal["target"])),
                total_text=motivation.format_goal_value(metric, total),
                target_text=motivation.format_goal_value(metric, goal["target"]),
                starts=goal["starts_on"].strftime("%d.%m.%Y"),
                ends=goal["ends_on"].strftime("%d.%m.%Y") if goal["ends_on"] else None,
                days_left=(goal["ends_on"] - today).days + 1 if goal["ends_on"] else None,
                reached=goal["reached_at"].strftime("%d.%m.%Y") if goal["reached_at"] else None,
                top=[{"name": visible[pid]["name"], "uuid": visible[pid]["uuid"],
                      "text": metrics_mod.format_value(metric, v)} for pid, v in top])


def best_indexes(values, lower_is_better):
    """Positions of the best value (none if all are equal, e.g. everyone at 0)."""
    if len(values) < 2 or len(set(values)) == 1:
        return set()
    best = min(values) if lower_is_better else max(values)
    return {i for i, value in enumerate(values) if value == best}


@server_bp.route("/vergleich")
@feature_rankings_required
def compare_page():
    period = selected_period()
    players = db().get_server_metrics(g.server["id"], PERIODS[period])
    by_name = {p["name"].lower(): p for p in players}
    selected = []
    for name in request.args.get("spieler", "").split(","):
        player = by_name.get(name.strip().lower())
        if player and player not in selected and len(selected) < MAX_COMPARED_PLAYERS:
            selected.append(player)
    for player, color in zip(selected, COMPARE_COLORS):
        player["color"] = color

    groups, wins = [], [0] * len(selected)
    for key, label in metrics_mod.GROUPS:
        rows = []
        for metric in (m for m in metrics_mod.METRICS if m.group == key):
            values = [p["values"][metric.key] for p in selected]
            best = best_indexes(values, metric.lower_is_better)
            for index in best:
                wins[index] += 1
            top = max(values, default=0)
            rows.append({"metric": metric, "best": best,
                         "cells": [{"text": metrics_mod.format_value(metric, v), "share": v / top if top else 0}
                                   for v in values]})
        groups.append((label, rows))

    details, history = [], None
    if selected:
        ids = [p["player_id"] for p in selected]
        for title, category in (("Meist abgebaute Blöcke", stats_mod.BLOCK_MINED),
                                ("Meist getötete Mobs", stats_mod.MOB_KILLED),
                                ("Getötet von", stats_mod.MOB_KILLED_BY)):
            rows = []
            for obj, values in db().get_top_objects(ids, category):
                row_values = [values.get(i, 0) for i in ids]
                rows.append({"label": metrics_mod.object_label(obj), "values": row_values,
                             "best": best_indexes(row_values, category == stats_mod.MOB_KILLED_BY)})
            details.append((title, rows))

        dates, series = db().get_metric_history(ids, HISTORY_DAYS)
        group_labels = dict(metrics_mod.GROUPS)
        scale = lambda metric, v: None if v is None else round(metrics_mod.scaled(metric, v), 2)
        history = {
            "dates": [d.isoformat() for d in dates],
            "players": [{"name": p["name"], "color": p["color"]} for p in selected],
            "metrics": [{"key": m.key, "label": m.label, "unit": m.unit, "group": group_labels[m.group],
                         "values": [[scale(m, v) for v in series[i][m.key]] for i in ids]}
                        for m in metrics_mod.METRICS],
            "since": (db().get_snapshot_start(g.server["id"]) or date.today()).isoformat(),
        }

    viewer = logged_in_player_id()
    mine = None
    if viewer:
        me = db().get_player_name_from_player_id(viewer)
        visible = {p["name"] for p in players}
        names = [n for n in [me] + [f["name"] for f in db().get_favorites(viewer)] if n in visible][:MAX_COMPARED_PLAYERS]
        if len(names) >= 2:
            mine = "/vergleich?spieler=" + ",".join(names)
    return render_template("vergleich.html", players=players, selected=selected, groups=groups, wins=wins, mine=mine,
                           metric_count=len(metrics_mod.METRICS), details=details, history=history,
                           period=period, periods=PERIODS, max_players=MAX_COMPARED_PLAYERS,
                           hint=period_hint(g.server["id"], PERIODS[period]),
                           prefixes=db().get_all_worn_prefixes(g.server["id"]))


def top_placements(players):
    """
    {player_id: [(rank, metric, row)]} of every player's places in the rankings, best first.
    Metrics where less is better (deaths) are left out: a top place there is no achievement.
    """
    places = {p["player_id"]: [] for p in players}
    ids = {p["name"]: p["player_id"] for p in players}
    for metric in (m for m in metrics_mod.METRICS if not m.lower_is_better):
        for row in ranked(players, metric):
            places[ids[row["name"]]].append((row["rank"], metric, row))
    order = {m.key: i for i, m in enumerate(metrics_mod.METRICS)}
    for player_id in places:
        places[player_id].sort(key=lambda place: (place[0], order[place[1].key]))
    return places


def player_list_page():
    players = db().get_server_metrics(g.server["id"], include_hidden=True)
    details = db().get_player_list_details(g.server["id"])
    prefixes = db().get_all_worn_prefixes(g.server["id"])
    places = top_placements([p for p in players if not p["hidden"]]) if current_app.config["FEATURE_RANKINGS"] else {}
    play_time = metrics_mod.METRICS_BY_KEY["play_time"]
    viewer = logged_in_player_id()
    favorites = {f["name"] for f in db().get_favorites(viewer)} if viewer else set()
    milestones = db().get_server_milestones(g.server["id"])
    rows = []
    for index, p in enumerate(players):
        info = details[p["player_id"]]
        if p["hidden"]:
            # only name, prefix, badges and whether the player is online right now
            info = dict(info, first_seen=None, last_seen=None)
            p = dict(p, values=dict(p["values"], play_time=0))
        rows.append({
            "hidden": p["hidden"], "favorite": p["name"] in favorites,
            "index": index, "name": p["name"], "uuid": p["uuid"], "online": p["online"],
            "prefix": prefixes.get(p["uuid"]),
            "first_seen": info["first_seen"], "last_seen": info["last_seen"],
            "moderator": info["moderator"], "is_op": info["is_op"], "banned": info["banned"],
            "badges": [] if p["hidden"] else [motivation.milestone_label(kind, value) for kind, value in
                                               sorted(milestones.get(p["player_id"], {}).items(), reverse=True)],
            "play_time": p["values"]["play_time"],
            "play_time_text": metrics_mod.format_value(play_time, p["values"]["play_time"]),
            "top": [(rank, metric.label) for rank, metric, _ in places.get(p["player_id"], []) if rank <= 3][:2],
        })
    return render_template("spieler.html", players=rows, online_count=sum(p["online"] for p in players),
                           logged_in=bool(viewer))


def player_extras(player_id):
    """Highlights, ranking places and daily activity for the player page."""
    database = db()
    highlights = []
    for label, category in (("Lieblingsblock", stats_mod.BLOCK_MINED), ("Meist getöteter Mob", stats_mod.MOB_KILLED),
                            ("Häufigste Todesursache", stats_mod.MOB_KILLED_BY)):
        top = database.get_top_objects([player_id], category, limit=1)
        if top:
            obj, values = top[0]
            count = values[player_id]
            suffix = {stats_mod.BLOCK_MINED: "abgebaut", stats_mod.MOB_KILLED: "getötet"}.get(category, "Tode")
            highlights.append({"label": label, "value": metrics_mod.object_label(obj),
                               "detail": f"{metrics_mod.format_count(count)} {suffix}"})
    moves = database.get_stat_values(player_id, stats_mod.CUSTOM, metrics_mod.MOVEMENT_LABELS)
    if moves:
        obj, cm = max(moves.items(), key=lambda item: item[1])
        highlights.append({"label": "Liebste Fortbewegung", "value": metrics_mod.MOVEMENT_LABELS[obj],
                           "detail": metrics_mod.format_distance(cm)})

    values = database.get_player_metrics(player_id)
    earned = database.get_player_achievements(player_id)
    achievements = []
    for achievement in achievements_mod.ACHIEVEMENTS:
        item = achievements_mod.progress(achievement, values[achievement.metric])
        at = earned.get((achievement.key, item["tier"]))
        item["earned"] = at.strftime("%d.%m.%Y") if at else None
        achievements.append(item)
    achievements.sort(key=lambda a: (-a["tier"], -a.get("next", {}).get("share", 1)))

    extras = {"highlights": highlights, "places": [], "activity": None, "achievements": achievements,
              "cabinet": trophy_cabinet(player_id),
              "achievement_count": sum(a["tier"] + 1 for a in achievements),
              "achievement_total": len(achievements_mod.ACHIEVEMENTS) * len(achievements_mod.TIERS)}
    if not current_app.config["FEATURE_RANKINGS"]:
        return extras

    players = list(visible_players(g.server["id"]).values())
    places = top_placements(players).get(player_id, [])
    extras["places"] = [{"rank": rank, "of": len(players), "label": metric.label, "text": row["text"]}
                        for rank, metric, row in places[:6]]

    dates, history = database.get_metric_history([player_id], 30)
    cumulative = history[player_id]["play_time"]
    days = []
    for i in range(1, len(dates)):
        before, after = cumulative[i - 1], cumulative[i]
        seconds = None if before is None or after is None else max(0, after - before) / 20
        days.append({"label": dates[i].strftime("%d.%m."), "tip": dates[i].strftime("%d.%m.%Y"),
                     "value": None if seconds is None else round(seconds / 3600, 2),
                     "text": "keine Daten" if seconds is None else format_time(seconds) if seconds else "nicht online"})
    if any(day["value"] is not None for day in days):
        extras["activity"] = {"days": days, "total": format_time(sum((d["value"] or 0) * 3600 for d in days)),
                              "active_days": sum(1 for d in days if d["value"])}
    return extras


def trophy_cabinet(player_id):
    """Competition places, players of the week, records held, streak and badges of a player (or None if empty)."""
    database = db()
    trophies = database.get_player_trophies(player_id)
    competitions = [dict(t, color=motivation.PLACE_COLORS.get(t["place"]), date=t["awarded_at"].strftime("%d.%m.%Y"))
                    for t in trophies if t["kind"] == "competition"]
    weeks = [t for t in trophies if t["kind"] == "player_of_week"]
    records = []
    for key, record in database.get_current_records(database.get_server_id_from_player_id(player_id)).items():
        metric = metrics_mod.METRICS_BY_KEY.get(key)
        if metric and record["player_id"] == str(player_id):
            records.append({"label": metric.label, "text": metrics_mod.format_value(metric, record["value"]),
                            "since": None if record["first"] else record["since"].strftime("%d.%m.%Y")})
    streak = database.get_player_streak(player_id)
    badges = [{"label": motivation.milestone_label(m["kind"], m["value"]), "kind": m["kind"],
               "date": m["reached_at"].strftime("%d.%m.%Y")} for m in database.get_player_milestones(player_id)]
    if not (competitions or weeks or records or badges or streak["best"] > 1):
        return None
    next_badge = next((n for n in motivation.STREAK_MILESTONES if n > streak["current"]), None)
    return {"competitions": competitions, "weeks": weeks, "records": records, "badges": badges, "streak": streak,
            "wins": sum(1 for t in competitions if t["place"] == 1), "next_badge": next_badge,
            "best_range": f"{streak['best_first'].strftime('%d.%m.')} – {streak['best_last'].strftime('%d.%m.%Y')}"
            if streak["best_first"] else None}


def duel_view(duel):
    metric = metrics_mod.METRICS_BY_KEY.get(duel["metric"])
    if metric is None:
        return None
    gain_c, gain_o = db().duel_gains(duel)
    top = max(gain_c, gain_o)
    leader = None if gain_c == gain_o else ("challenger" if gain_c > gain_o else "opponent")
    fmt = lambda ts: ts.strftime("%d.%m.%Y, %H:%M") if ts else None
    return dict(duel, metric_label=metric.label, leader=leader,
                challenger_text=metrics_mod.format_value(metric, gain_c), opponent_text=metrics_mod.format_value(metric, gain_o),
                challenger_share=gain_c / top if top else 0, opponent_share=gain_o / top if top else 0,
                created=fmt(duel["created_at"]), starts=fmt(duel["starts_at"]), ends=fmt(duel["ends_at"]))


DUEL_STATUS = {"pending": "wartet auf Antwort", "running": "läuft", "finished": "beendet",
               "declined": "abgelehnt", "expired": "abgelaufen", "cancelled": "zurückgezogen"}


@server_bp.route("/duelle")
@feature_rankings_required
def duels_page():
    viewer = logged_in_player_id()
    duels = [v for v in (duel_view(d) for d in db().list_duels(g.server["id"])) if v]
    players = [p for p in db().get_server_metrics(g.server["id"]) if not viewer or p["player_id"] != str(viewer)]
    return render_template("duelle.html", duels=duels, viewer=str(viewer) if viewer else None,
                           status_labels=DUEL_STATUS, players=players,
                           metrics=[m for m in metrics_mod.METRICS if not m.lower_is_better], metric_groups=metrics_mod.GROUPS,
                           can_duel=bool(viewer) and not db().is_stats_hidden(viewer))


@server_bp.route("/melden")
@player_required
def report_page():
    return render_template("melden.html", players=db().get_players_overview_from_subdomain(g.subdomain),
                           reason_max=REPORT_REASON_MAX)


################################ YEAR IN REVIEW #################################

def year_rank(gains, visible, player_id, key):
    """(rank, of) of a player by the year's gain of a metric among the visible players with a gain."""
    values = sorted(((values.get(key, 0), pid) for pid, values in gains.items() if pid in visible and values.get(key, 0) > 0),
                    reverse=True)
    for index, (value, pid) in enumerate(values):
        if pid == player_id:
            return next(i for i, (v, _) in enumerate(values) if v == value) + 1, len(values)
    return None, len(values)


def year_review(player_id, year):
    """Slides of a player's year: [{"kind", "kicker", "title", "big", "lines", "rank"}] and a summary."""
    database = db()
    server_id = g.server["id"]
    gains, first_day = database.get_year_gains(server_id, year)
    mine = gains.get(player_id, {})
    visible = set(visible_players(server_id))
    m = metrics_mod.METRICS_BY_KEY
    fmt = lambda key: metrics_mod.format_value(m[key], mine.get(key, 0))
    rank_text = lambda key: (lambda r: f"Platz {r[0]} von {r[1]} auf dem Server" if r[0] else None)(
        year_rank(gains, visible, player_id, key))
    sessions = database.get_year_sessions(player_id, year)
    events = database.get_year_events(player_id, year)
    slides = []

    if mine.get("play_time") or sessions["days"]:
        lines = []
        if sessions["days"]:
            lines.append(f"An {len(sessions['days'])} Tagen online, {sessions['sessions']} Mal eingeloggt.")
        if sessions["longest"] >= 60:
            lines.append(f"Längste Sitzung: {format_time(sessions['longest'])}")
        if sessions["weekday"] is not None:
            lines.append(f"Am liebsten {WEEKDAY_NAMES[sessions['weekday']]}s gegen {sessions['hour']} Uhr.")
        slides.append({"kind": "time", "kicker": "Spielzeit", "big": fmt("play_time"), "title": "hast du gespielt",
                       "lines": lines, "rank": rank_text("play_time")})
    if mine.get("blocks_mined"):
        lines = [f"Darunter {fmt('diamonds')} Diamanterz und {fmt('ancient_debris')} Ancient Debris."
                 if mine.get("diamonds") or mine.get("ancient_debris") else "Kein einziges Diamanterz – nächstes Jahr!"]
        if mine.get("blocks_placed"):
            lines.append(f"Und {fmt('blocks_placed')} Blöcke platziert.")
        slides.append({"kind": "mining", "kicker": "Abbau", "big": fmt("blocks_mined"), "title": "Blöcke abgebaut",
                       "lines": lines, "rank": rank_text("blocks_mined")})
    if mine.get("distance"):
        km = mine["distance"] / 100_000
        lines = [f"Davon {fmt('distance_elytra')} mit der Elytra." if mine.get("distance_elytra") else "Alles ohne Elytra."]
        if km >= motivation.MARATHON_KM:
            lines.append(f"Das sind {metrics_mod.format_count(int(km / motivation.MARATHON_KM))} Marathons.")
        if mine.get("jumps"):
            lines.append(f"{fmt('jumps')} Mal gesprungen.")
        slides.append({"kind": "travel", "kicker": "Unterwegs", "big": fmt("distance"), "title": "zurückgelegt",
                       "lines": lines, "rank": rank_text("distance")})
    if mine.get("mob_kills") or mine.get("deaths"):
        lines = [f"{fmt('deaths')} Mal gestorben."] if mine.get("deaths") else ["Kein einziges Mal gestorben!"]
        if mine.get("fish_caught"):
            lines.append(f"Nebenbei {fmt('fish_caught')} Fische geangelt.")
        slides.append({"kind": "fight", "kicker": "Abenteuer", "big": fmt("mob_kills"), "title": "Mobs besiegt",
                       "lines": lines, "rank": rank_text("mob_kills")})
    if events["achievements"]:
        best = {}
        for key, tier, _ in events["achievements"]:
            best[key] = max(best.get(key, -1), tier)
        top = sorted(best.items(), key=lambda item: -item[1])[:4]
        slides.append({"kind": "achievements", "kicker": "Erfolge", "big": str(len(events["achievements"])),
                       "title": "Erfolgsstufen erreicht",
                       "lines": [f"{achievements_mod.ACHIEVEMENTS_BY_KEY[k].name}: {achievements_mod.TIERS[t][1]}"
                                 for k, t in top if k in achievements_mod.ACHIEVEMENTS_BY_KEY], "rank": None})
    honours = []
    for t in events["trophies"]:
        honours.append(f"{t['place']}. Platz im Wettbewerb »{t['title']}«" if t["kind"] == "competition"
                       else f"Spieler der Woche ({t['title']})")
    for metric_key, _, _ in events["records"]:
        if metric_key in m:
            honours.append(f"Rekord geholt: {m[metric_key].label}")
    for milestone in events["milestones"]:
        honours.append(motivation.milestone_label(milestone["kind"], milestone["value"]))
    if honours:
        slides.append({"kind": "honours", "kicker": "Ruhm", "big": str(len(honours)),
                       "title": "Auszeichnungen" if len(honours) != 1 else "Auszeichnung",
                       "lines": list(dict.fromkeys(honours))[:6], "rank": None})
    summary = [(m[key].label, fmt(key)) for key in ("play_time", "blocks_mined", "distance", "mob_kills", "deaths")
               if mine.get(key)]
    return slides, summary, first_day


@server_bp.route("/rueckblick/<path:player_name>")
def year_review_page(player_name):
    player_id = db().get_player_id_from_player_name_and_server_id(player_name, g.server["id"])
    if player_id is None:
        abort(404)
    info = db().get_player_info_by_player_id(player_id)
    viewer = logged_in_player_id()
    if info["hide_stats"] and str(viewer) != str(player_id):
        abort(404)
    today = db().get_today()
    try:
        year = int(request.args.get("jahr", today.year))
    except ValueError:
        abort(404)
    if not 2020 <= year <= today.year:
        abort(404)
    slides, summary, first_day = year_review(str(player_id), year)
    since = first_day.strftime("%d.%m.") if first_day and (first_day.month, first_day.day) != (1, 1) else None
    return render_template("rueckblick.html", player=info["name"], uuid=str(info["mojang_uuid"]), year=year,
                           slides=slides, summary=summary, since=since, running=year == today.year,
                           share_url=url_for("server.year_review_page", subdomain=g.subdomain, player_name=info["name"],
                                             jahr=year, _external=True))


################################ ACCESS, RULES #################################

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
APPLICATION_MAX_LENGTH = 500


@server_bp.route("/mitmachen")
def access_page():
    settings = db().get_server_settings(g.server["id"])
    if settings["access_mode"] == "off":
        abort(404)
    return render_template("mitmachen.html", mode=settings["access_mode"], message_max=APPLICATION_MAX_LENGTH)


@server_bp.route("/api/access/apply", methods=["POST"])
def access_apply_api():
    if not request.is_json:
        return {"error": "json required"}, 415
    if db().get_server_settings(g.server["id"])["access_mode"] not in ("application", "both"):
        abort(404)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    message = " ".join(str(data.get("message") or "").split())
    if not MC_NAME_RE.match(name):
        return {"error": "Bitte deinen Minecraft-Namen angeben (3-16 Zeichen, Buchstaben, Zahlen und _)."}, 400
    if not 10 <= len(message) <= APPLICATION_MAX_LENGTH:
        return {"error": f"Erzähl kurz etwas über dich (10-{APPLICATION_MAX_LENGTH} Zeichen)."}, 400
    # the number of open applications is capped, so one client must not be able to fill them all
    if not rate_limit("application", request.remote_addr):
        return {"error": "Zu viele Bewerbungen von deinem Anschluss. Versuche es später noch einmal."}, 429
    request_id, error = db().add_application(g.server["id"], name, message)
    if error == "pending":
        return {"error": "Für diesen Namen gibt es schon eine offene Bewerbung."}, 409
    if error == "accepted":
        return {"error": "Dieser Name ist schon freigeschaltet."}, 409
    if error == "full":
        return {"error": "Gerade sind zu viele Bewerbungen offen. Versuche es später noch einmal."}, 429
    moderators = db().get_online_moderator_uuids(g.server["id"])
    if moderators:
        db().notify_server_event(g.server["id"], "tell", uuids=moderators,
                                 text=f"&7[Bewerbung] &f{name} &7möchte mitspielen – siehe Verwaltung.")
    return ("", 200)


@server_bp.route("/api/access/redeem", methods=["POST"])
def access_redeem_api():
    if not request.is_json:
        return {"error": "json required"}, 415
    if db().get_server_settings(g.server["id"])["access_mode"] not in ("code", "both"):
        abort(404)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    code = str(data.get("code") or "").strip()
    if not MC_NAME_RE.match(name):
        return {"error": "Bitte deinen Minecraft-Namen angeben (3-16 Zeichen, Buchstaben, Zahlen und _)."}, 400
    if not rate_limit("invite_code", request.remote_addr):
        return {"error": "Zu viele Versuche. Warte ein paar Minuten."}, 429
    result = db().redeem_invite_code(g.server["id"], name, code[:40])
    if result == "invalid":
        return {"error": "Dieser Code ist ungültig oder abgelaufen."}, 400
    if result == "already":
        return {"error": "Dieser Name ist schon freigeschaltet."}, 409
    db().notify_server_event(g.server["id"], "whitelist")
    return ("", 200)


def faq_entries(text):
    """"Question?\nAnswer ..." blocks separated by empty lines -> [(question, answer)]."""
    entries = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if lines:
            entries.append((lines[0], "\n".join(lines[1:])))
    return entries


@server_bp.route("/regeln")
def rules_page():
    settings = db().get_server_settings(g.server["id"])
    if not settings["rules_enabled"]:
        abort(404)
    return render_template("regeln.html", rules=settings["rules"] or "", faq=faq_entries(settings["faq"]))


################################ COMMUNITY: events, polls, build gallery, guestbook #################################

WEEKDAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def event_view(event, mine=()):
    at = event["starts_at"]
    now = datetime.now(timezone.utc)
    return dict(event, day=f"{WEEKDAY_NAMES[at.weekday()]}, {at.strftime('%d.%m.%Y')}", time=at.strftime("%H:%M"),
                date_short=at.strftime("%d.%m."), month=MONTHS[at.month - 1], day_number=at.day,
                running=at <= now, signed_up=event["id"] in mine,
                soon=timedelta(0) < at - now < timedelta(hours=24))


def poll_view(poll, mine=None):
    top = max(poll["votes"], default=0)
    return dict(poll, ends=poll["ends_at"].strftime("%d.%m.%Y, %H:%M"), my_vote=(mine or {}).get(poll["id"]),
                rows=[{"index": i, "text": option, "votes": v, "share": v / poll["total"] if poll["total"] else 0,
                       "percent": round(v * 100 / poll["total"]) if poll["total"] else 0, "top": v == top and v > 0}
                      for i, (option, v) in enumerate(zip(poll["options"], poll["votes"]))])


def build_view(build, liked=()):
    return dict(build, url=image_url(build["filename"]), date=build["created_at"].strftime("%d.%m.%Y"),
                liked=build["id"] in liked)


@server_bp.route("/events")
def events_page():
    viewer = logged_in_player_id()
    mine = db().get_player_event_ids(viewer) if viewer else set()
    upcoming = []
    for event in db().list_events(g.server["id"]):
        view = event_view(event, mine)
        view["people"] = db().get_event_signups(event["id"])
        upcoming.append(view)
    return render_template("events.html", upcoming=upcoming, logged_in=bool(viewer),
                           past=[event_view(e) for e in db().list_events(g.server["id"], upcoming=False, limit=10)])


@server_bp.route("/umfragen")
def polls_page():
    viewer = logged_in_player_id()
    mine = db().get_player_votes(viewer) if viewer else {}
    polls = [poll_view(p, mine) for p in db().list_polls(g.server["id"])]
    return render_template("umfragen.html", open_polls=[p for p in polls if p["open"]],
                           closed_polls=[p for p in polls if not p["open"]], logged_in=bool(viewer))


@server_bp.route("/galerie")
def gallery_page():
    viewer = logged_in_player_id()
    order = "top" if request.args.get("sortierung") == "beliebt" else "new"
    liked = db().get_liked_builds(viewer) if viewer else set()
    mine = [build_view(b) for b in db().list_builds(g.server["id"], "pending", player_id=viewer)] if viewer else []
    return render_template("galerie.html", builds=[build_view(b, liked) for b in db().list_builds(g.server["id"], order=order)],
                           order=order, waiting=mine, viewer=str(viewer) if viewer else None,
                           moderator=bool(viewer) and db().is_moderator(viewer),
                           max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024))


@server_bp.route("/api/events/signup", methods=["POST"])
@player_required
def event_signup_api():
    try:
        event_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Event."}, 400
    signed_up = db().toggle_event_signup(g.server["id"], event_id, logged_in_player_id())
    if signed_up is None:
        return {"error": "Dieses Event ist schon vorbei."}, 404
    return {"signed_up": signed_up, "people": db().get_event_signups(event_id)}


@server_bp.route("/api/polls/vote", methods=["POST"])
@player_required
def poll_vote_api():
    data = request.get_json(silent=True) or {}
    try:
        poll_id, option = int(data.get("id")), int(data.get("option"))
    except (TypeError, ValueError):
        return {"error": "Ungültige Stimme."}, 400
    result = db().vote(g.server["id"], poll_id, logged_in_player_id(), option)
    if result == "closed":
        return {"error": "Die Umfrage ist schon vorbei."}, 409
    if result != "ok":
        return {"error": "Ungültige Stimme."}, 400
    return ("", 200)


BUILD_TITLE_MAX, BUILD_TEXT_MAX, BUILD_COORDS_MAX = 60, 300, 40


@server_bp.route("/api/builds", methods=["POST"])
@player_required(allow_upload=True)
def build_upload_api():
    title = " ".join(str(request.form.get("title") or "").split())
    description = " ".join(str(request.form.get("description") or "").split()) or None
    coordinates = " ".join(str(request.form.get("coordinates") or "").split()) or None
    file = request.files.get("image")
    if not 3 <= len(title) <= BUILD_TITLE_MAX:
        return {"error": f"Der Titel muss 3-{BUILD_TITLE_MAX} Zeichen lang sein."}, 400
    if description and len(description) > BUILD_TEXT_MAX or coordinates and len(coordinates) > BUILD_COORDS_MAX:
        return {"error": "Beschreibung oder Koordinaten sind zu lang."}, 400
    if file is None:
        return {"error": "Bitte ein Bild auswählen."}, 400
    try:
        filename = save_image(file.stream, "gallery", config.UPLOAD_DIR)
    except InvalidImage as e:
        return {"error": str(e)}, 400
    player_id = logged_in_player_id()
    build_id = db().add_build(g.server["id"], player_id, title, filename, description, coordinates)
    if build_id is None:
        delete_images([filename], config.UPLOAD_DIR)
        return {"error": "Du hast schon genug Bilder, die auf Freigabe warten (oder heute hochgeladen). "
                         "Warte, bis die Moderatoren sie angesehen haben."}, 429
    moderators = db().get_online_moderator_uuids(g.server["id"])
    if moderators:
        db().notify_server_event(g.server["id"], "tell", uuids=moderators,
                                 text=f"&7[Galerie] &f{db().get_player_name_from_player_id(player_id)} &7hat »{game_commands.clean(title)}« "
                                      "hochgeladen – bitte in der Verwaltung freigeben.")
    return {"id": build_id}, 201


@server_bp.route("/api/builds/like", methods=["POST"])
@player_required
def build_like_api():
    try:
        build_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Bild."}, 400
    liked = db().toggle_build_like(g.server["id"], build_id, logged_in_player_id())
    if liked is None:
        return {"error": "Eigene Bilder kannst du nicht liken."}, 409
    return {"liked": liked, "likes": db().get_build(build_id)["likes"]}


@server_bp.route("/api/builds/delete", methods=["POST"])
@player_required
def build_delete_api():
    try:
        build_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Bild."}, 400
    deleted = db().delete_build(g.server["id"], build_id, logged_in_player_id())
    if deleted is None:
        return {"error": "Unbekanntes Bild."}, 404
    delete_images([deleted[1]], config.UPLOAD_DIR)
    return ("", 200)


GUESTBOOK_MAX_LENGTH = 300


@server_bp.route("/api/guestbook", methods=["POST"])
@player_required
def guestbook_add_api():
    data = request.get_json(silent=True) or {}
    text = " ".join(str(data.get("text") or "").split())
    if not 2 <= len(text) <= GUESTBOOK_MAX_LENGTH:
        return {"error": f"Der Eintrag muss 2-{GUESTBOOK_MAX_LENGTH} Zeichen lang sein."}, 400
    player_id = db().get_player_id_from_player_name_and_server_id(str(data.get("name") or ""), g.server["id"])
    if player_id is None or db().is_stats_hidden(player_id):
        return {"error": "Unbekannter Spieler."}, 404
    if db().add_guestbook_entry(player_id, logged_in_player_id(), text) is None:
        return {"error": "Du hast in der letzten Stunde schon genug geschrieben."}, 429
    return ("", 200)


@server_bp.route("/api/guestbook/delete", methods=["POST"])
@player_required
def guestbook_delete_api():
    try:
        entry_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannter Eintrag."}, 400
    viewer = logged_in_player_id()
    moderator = db().is_moderator(viewer)
    entry = db().delete_guestbook_entry(g.server["id"], entry_id, None if moderator else viewer)
    if entry is None:
        return {"error": "Unbekannter Eintrag."}, 404
    if moderator and str(viewer) not in (entry["author_id"], entry["player_id"]):
        db().add_mod_log(g.server["id"], db().get_player_name_from_player_id(viewer), "guestbook_delete",
                         entry["author"], f"auf der Seite von {entry['owner']}: {entry['text'][:150]}")
    return ("", 200)


@server_bp.route("/api/guestbook/report", methods=["POST"])
@player_required
def guestbook_report_api():
    try:
        entry_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannter Eintrag."}, 400
    if not db().report_guestbook_entry(g.server["id"], entry_id):
        return {"error": "Unbekannter Eintrag."}, 404
    return ("", 200)


BIO_MAX_LENGTH = 160


@server_bp.route("/profil")
def profile_page():
    player_id = logged_in_player_id()
    if not player_id:
        return redirect("/login?next=/profil")
    return render_template("profil.html", profile=db().get_profile(player_id), bio_max=BIO_MAX_LENGTH,
                           favorites=db().get_favorites(player_id), sidebar=db().get_sidebar(player_id))


@server_bp.route("/spieler/<path:player_name>/karte.png")
def player_card(player_name):
    """Shareable image with the player's top values (also used as og:image)."""
    player_id = db().get_player_id_from_player_name_and_server_id(player_name, g.server["id"])
    if player_id is None or db().is_stats_hidden(player_id):
        abort(404)
    from web.card import render_card
    info = db().get_player_info_by_player_id(player_id)
    values = db().get_player_metrics(player_id)
    places = top_placements(list(visible_players(g.server["id"]).values())).get(str(player_id), [])
    tiles = [(m.label, metrics_mod.format_value(m, values[m.key]))
             for m in (metrics_mod.METRICS_BY_KEY[k] for k in ("play_time", "blocks_mined", "mob_kills", "distance"))]
    ranks = [f"#{rank} {metric.label}" for rank, metric, _ in places if rank <= 3][:3]
    achieved = sum(1 for a in achievements_mod.ACHIEVEMENTS
                   if achievements_mod.tier_of(a, values[a.metric]) >= 0)
    png = render_card(name=info["name"], uuid=str(info["mojang_uuid"]), server_name=g.server["server_name"],
                      prefix=db().get_player_prefix(player_id), prefix_colors=PREFIX_COLORS, tiles=tiles,
                      ranks=ranks, achievements=f"{achieved} von {len(achievements_mod.ACHIEVEMENTS)} Erfolgen")
    response = Response(png, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=1800"
    return response


@server_bp.route("/api/spieler/<path:player_name>/erfolge")
def player_achievements_api(player_name):
    """The rendered achievements of a player, for the live update of the player page."""
    player_id = db().get_player_id_from_player_name_and_server_id(player_name, g.server["id"])
    if player_id is None or (db().is_stats_hidden(player_id) and str(logged_in_player_id()) != str(player_id)):
        abort(404)
    extras = player_extras(str(player_id))
    return {"count": extras["achievement_count"], "html": render_template("_achievements.html", extras=extras)}


@server_bp.route("/api/meine-erfolge")
def my_new_achievements_api():
    """Tiers the logged in player reached after ?seit=<iso time> (for the toast on every page)."""
    player_id = logged_in_player_id()
    now = datetime.now(timezone.utc)
    if not player_id:
        return {"now": now.isoformat(), "new": []}
    try:
        since = datetime.fromisoformat(request.args.get("seit", ""))
    except ValueError:
        return {"now": now.isoformat(), "new": []}
    new = []
    for key, tier in db().get_new_achievements(player_id, since):
        achievement = achievements_mod.ACHIEVEMENTS_BY_KEY.get(key)
        if achievement:
            new.append({"name": achievement.name, "tier": achievements_mod.TIERS[tier][1],
                        "tier_key": achievements_mod.TIERS[tier][0], "description": achievement.description})
    return {"now": now.isoformat(), "new": new}


FEED_TEXTS = {
    "join": "ist online gekommen",
    "new": "ist neu auf dem Server – willkommen!",
}


@server_bp.route("/api/feed")
@feature_rankings_required
def feed_api():
    events = []
    for e in db().get_feed(g.server["id"]):
        if e["kind"] in FEED_TEXTS:
            text = FEED_TEXTS[e["kind"]]
        elif e["kind"] == "achievement":
            achievement = achievements_mod.ACHIEVEMENTS_BY_KEY.get(e["detail"])
            if achievement is None:
                continue
            text = f"hat den Erfolg »{achievement.name}« ({achievements_mod.TIERS[e['tier']][1]}) erreicht"
        elif e["kind"] == "record":
            metric = metrics_mod.METRICS_BY_KEY.get(e["detail"])
            if metric is None:
                continue
            text = f"hat den Rekord »{metric.label}« geholt"
        elif e["kind"] == "streak":
            text = f"war {e['detail']} Tage in Folge online"
        elif e["kind"] == "anniversary":
            text = f"ist seit {motivation.years_text(int(e['detail']))} dabei"
        elif e["kind"] == "goal":
            text = f"Gemeinschaftsziel »{e['detail']}« geschafft!"
        elif e["kind"] == "player_of_week":
            text = f"ist Spieler der Woche ({e['detail']})"
        elif e["kind"] == "competition_start":
            text = f"Wettbewerb »{e['detail']}« hat begonnen"
        else:
            text = f"Wettbewerb »{e['detail']}« ist vorbei"
        events.append({"at": e["at"].isoformat(), "kind": e["kind"], "name": e["name"], "uuid": e["uuid"],
                       "text": text, "tier": achievements_mod.TIERS[e["tier"]][0] if e["tier"] is not None else None})
    return {"events": events}


@server_bp.route("/add_pref")
@player_required
def prefix_edit_page():
    player_id = logged_in_player_id()
    is_moderator = db().is_moderator(player_id)
    colors = {name: hex_color for name, hex_color in PREFIX_COLORS.items()
              if is_moderator or name not in MODERATOR_PREFIX_COLORS}
    return render_template("prefix_edit.html", own_prefix=db().get_owned_prefix(player_id),
                           current_prefix=db().get_player_prefix(player_id), selectable_colors=colors,
                           is_moderator=is_moderator)


@server_bp.route("/join_pref")
@player_required
def prefix_join_page():
    player_id = logged_in_player_id()
    return render_template("prefix_join.html", prefixes=db().list_prefixes(g.server["id"]),
                           current_prefix=db().get_player_prefix(player_id))


@server_bp.route("/users")
@moderator_required
def moderation_page():
    today = db().get_today()
    return render_template("moderation.html", ban_reasons=db().get_ban_reasons(), bans_api="/api/mod",
                           players=db().get_players_overview_from_subdomain(g.subdomain),
                           activity=player_activity(g.server["id"]),
                           competitions=[v for v in (competition_view(c, today) for c in db().list_competitions(g.server["id"])) if v]
                           if current_app.config["FEATURE_RANKINGS"] else None,
                           metrics=metrics_mod.METRICS, metric_groups=metrics_mod.GROUPS,
                           goals=[v for v in (goal_view(goal, today) for goal in db().list_goals(g.server["id"])) if v]
                           if current_app.config["FEATURE_RANKINGS"] else None,
                           goal_units={m.key: motivation.goal_unit(m) for m in metrics_mod.METRICS},
                           today=today, default_end=today + timedelta(days=6),
                           log=mod_log_view(g.server["id"]), health=health_view(g.server["id"]),
                           settings=db().get_server_settings(g.server["id"]),
                           access_requests=[dict(r, when=r["created_at"].strftime("%d.%m.%Y, %H:%M"))
                                            for r in db().list_access_requests(g.server["id"], 60)],
                           invite_codes=[dict(c, until=c["expires_at"].strftime("%d.%m.%Y") if c["expires_at"] else None)
                                         for c in db().list_invite_codes(g.server["id"])],
                           xray=xray_hints(g.server["id"]),
                           events=[event_view(e) for e in db().list_events(g.server["id"])],
                           polls=[poll_view(p) for p in db().list_polls(g.server["id"], limit=10)],
                           pending_builds=[build_view(b) for b in db().list_builds(g.server["id"], "pending")],
                           reported_entries=[dict(e, when=e["created_at"].strftime("%d.%m.%Y, %H:%M"))
                                             for e in db().get_reported_guestbook_entries(g.server["id"])],
                           now_local=datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%dT%H:%M"),
                           reports=[dict(r, when=r["created_at"].strftime("%d.%m.%Y, %H:%M"),
                                         handled=r["handled_at"].strftime("%d.%m.%Y, %H:%M") if r["handled_at"] else None)
                                    for r in db().list_reports(g.server["id"], include_handled=True, limit=50)])


MOD_LOG_ACTIONS = {
    "ban": "hat gebannt", "unban": "hat entbannt", "ingame_ban": "im Spiel gebannt", "ingame_unban": "im Spiel entbannt",
    "ingame_pardon": "Website-Bann im Spiel aufgehoben",
    "mod_add": "zum Moderator gemacht", "mod_remove": "Moderatorrechte entzogen",
    "competition_create": "Wettbewerb angelegt", "competition_delete": "Wettbewerb gelöscht",
    "note_add": "Notiz geschrieben", "note_delete": "Notiz gelöscht",
    "goal_create": "Gemeinschaftsziel angelegt", "goal_delete": "Gemeinschaftsziel gelöscht",
    "report_resolve": "Meldung erledigt",
    "event_create": "Event angelegt", "event_delete": "Event gelöscht",
    "poll_create": "Umfrage angelegt", "poll_delete": "Umfrage gelöscht",
    "build_approve": "Galeriebild freigegeben", "build_delete": "Galeriebild gelöscht",
    "guestbook_delete": "Gästebucheintrag gelöscht", "guestbook_keep": "Gästebucheintrag behalten",
    "warn": "verwarnt", "warning_delete": "Verwarnung gelöscht", "mute": "stummgeschaltet", "unmute": "Stummschaltung aufgehoben",
    "application_accept": "Bewerbung angenommen", "application_reject": "Bewerbung abgelehnt",
    "code_create": "Einladungscode angelegt", "code_delete": "Einladungscode gelöscht", "settings": "Einstellungen geändert",
}


# X-ray hints: ore blocks per 1000 stone/deepslate/tuff (diamonds) and per 1000 netherrack (debris).
# Normal mining gives roughly 1-3 diamond ore per 1000 blocks; players who mine mostly in caves can be
# higher, so this is only a hint for moderators.
XRAY_MIN_STONE, XRAY_MIN_DIAMONDS, XRAY_DIAMOND_RATE = 2000, 15, 6.0
XRAY_MIN_NETHERRACK, XRAY_MIN_DEBRIS, XRAY_DEBRIS_RATE = 1000, 8, 8.0


def xray_hints(server_id):
    """Players with an unusually high ore rate, highest first: [{"name", "uuid", "diamond_rate", "debris_rate", ...}]."""
    hints = []
    for p in db().get_mining_ratios(server_id):
        diamond_rate = p["diamonds"] * 1000 / p["stone"] if p["stone"] else 0
        debris_rate = p["debris"] * 1000 / p["netherrack"] if p["netherrack"] else 0
        flags = []
        if p["stone"] >= XRAY_MIN_STONE and p["diamonds"] >= XRAY_MIN_DIAMONDS and diamond_rate >= XRAY_DIAMOND_RATE:
            flags.append("diamonds")
        if p["netherrack"] >= XRAY_MIN_NETHERRACK and p["debris"] >= XRAY_MIN_DEBRIS and debris_rate >= XRAY_DEBRIS_RATE:
            flags.append("debris")
        if flags:
            hints.append(dict(p, flags=flags, diamond_rate=f"{diamond_rate:.1f}".replace(".", ","),
                              debris_rate=f"{debris_rate:.1f}".replace(".", ","),
                              score=max(diamond_rate / XRAY_DIAMOND_RATE, debris_rate / XRAY_DEBRIS_RATE),
                              diamonds_per_hour=f"{p['diamonds'] / p['hours']:.1f}".replace(".", ",") if p["hours"] >= 1 else "–"))
    return sorted(hints, key=lambda h: -h["score"])


def mod_log_view(server_id, target_name=None, limit=100):
    return [dict(entry, label=MOD_LOG_ACTIONS.get(entry["action"], entry["action"]),
                 when=entry["at"].strftime("%d.%m.%Y, %H:%M"))
            for entry in db().get_mod_log(server_id, limit=limit, target_name=target_name)]


# TPS thresholds for the status (a healthy server has 20)
TPS_GOOD, TPS_WARNING = 19.0, 15.0


def format_duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} {'Tag' if days == 1 else 'Tage'} {hours} Std."
    return f"{hours} Std. {rest // 60} Min." if hours else f"{rest // 60} Min."


def health_view(server_id):
    """Current health, 24 h history and availability for the moderation page (None before the first sample)."""
    latest = db().get_latest_health(server_id)
    server = db().get_server_information_dict(g.subdomain)
    online = db().is_plugin_online(server_id)
    offline_since = None if online or not server.get("plugin_last_seen") else \
        server["plugin_last_seen"].strftime("%d.%m.%Y, %H:%M Uhr")
    if latest is None or latest["at"] < datetime.now(timezone.utc) - timedelta(minutes=10):
        # connected but no (recent) sample: the plugin is older than 3.2 or the socket server is outdated
        return {"online": online, "offline_since": offline_since, "latest": None, "stale": latest is not None,
                "last_sample": latest["at"].strftime("%d.%m.%Y, %H:%M Uhr") if latest else None}
    tps = latest["tps"]
    status = (None if tps is None else "good" if tps >= TPS_GOOD else "warning" if tps >= TPS_WARNING else "critical")
    history = db().get_health_history(server_id, 24, 10)
    fmt = lambda ts: ts.strftime("%d.%m. %H:%M")
    availability = db().get_health_availability(server_id)
    return {
        "online": online, "offline_since": offline_since, "latest": latest, "status": status,
        "tps": None if tps is None else f"{tps:.1f}".replace(".", ","),
        "uptime": format_duration(latest["uptime_s"]) if latest["uptime_s"] is not None else "–",
        "sampled": latest["at"].strftime("%H:%M Uhr"),
        "availability": None if availability is None else f"{availability * 100:.1f} %".replace(".", ","),
        "tps_chart": {"unit": "", "max": 20, "points": [{"t": fmt(t), "v": None if v is None else round(float(v), 2)}
                                                        for t, v, _, _ in history]},
        "mem_chart": {"unit": " MB", "max": latest["mem_max_mb"],
                      "points": [{"t": fmt(t), "v": None if v is None else round(float(v))} for t, _, v, _ in history]},
    }


def player_activity(server_id):
    """All players with first/last seen and play time, longest absent first (for moderators)."""
    details = db().get_player_list_details(server_id)
    play_time = metrics_mod.METRICS_BY_KEY["play_time"]
    rows = []
    for p in db().get_server_metrics(server_id, include_hidden=True):
        info = details[p["player_id"]]
        last = None if p["online"] else info["last_seen"]
        rows.append({"name": p["name"], "uuid": p["uuid"], "online": p["online"], "banned": info["banned"],
                     "last_seen": last, "first_seen": info["first_seen"],
                     "play_time": metrics_mod.format_value(play_time, p["values"]["play_time"])})
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r["online"], r["last_seen"] or far_past))
    return rows


@server_bp.route("/api/profile", methods=["POST"])
@player_required
def profile_save_api():
    data = request.get_json(silent=True) or {}
    bio = " ".join(str(data.get("bio") or "").split())  # one line, no control characters
    if len(bio) > BIO_MAX_LENGTH:
        return {"error": f"Der Text darf höchstens {BIO_MAX_LENGTH} Zeichen lang sein."}, 400
    db().save_profile(logged_in_player_id(), bio, bool(data.get("hide_stats")))
    return ("", 200)


SIDEBAR_MODES = ("off", "competition", "playtime")


@server_bp.route("/api/sidebar", methods=["POST"])
@player_required
def sidebar_save_api():
    mode = str((request.get_json(silent=True) or {}).get("mode") or "")
    if mode not in SIDEBAR_MODES:
        return {"error": "Unbekannte Einstellung."}, 400
    player_id = logged_in_player_id()
    db().set_sidebar(player_id, mode)
    db().notify_server_event(g.server["id"], "sidebar", player_id=str(player_id))
    return ("", 200)


@server_bp.route("/api/duels", methods=["POST"])
@player_required
def duel_create_api():
    data = request.get_json(silent=True) or {}
    metric = metrics_mod.METRICS_BY_KEY.get(str(data.get("metric") or ""))
    if metric is None or metric.lower_is_better:
        return {"error": "Unbekannte Kennzahl."}, 400
    try:
        days = int(data.get("days"))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= 7:
        return {"error": "Ein Duell dauert 1 bis 7 Tage."}, 400
    opponent_id = db().get_player_id_from_player_name_and_server_id(str(data.get("name") or ""), g.server["id"])
    if opponent_id is None:
        return {"error": "Unbekannter Spieler."}, 404
    player_id = logged_in_player_id()
    duel_id, error = db().create_duel(g.server["id"], player_id, opponent_id, metric.key, days)
    if error:
        return {"error": {"self": "Du kannst dich nicht selbst herausfordern.",
                          "hidden": "Duelle gehen nur, wenn ihr beide eure Statistiken öffentlich zeigt.",
                          "open": "Mit diesem Spieler hast du schon ein offenes Duell."}[error]}, 409
    name = db().get_player_name_from_player_id(player_id)
    db().notify_server_event(g.server["id"], "tell", uuids=[str(db().get_mojang_uuid_from_player_id(opponent_id))],
                             text=game_commands.challenge_text(name, metric, days))
    return {"id": duel_id}, 200


@server_bp.route("/api/duels/cancel", methods=["POST"])
@player_required
def duel_cancel_api():
    try:
        duel_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Duell."}, 400
    duel = db().cancel_duel(duel_id, logged_in_player_id())
    if duel is None:
        return {"error": "Diese Herausforderung kannst du nicht mehr zurückziehen."}, 404
    db().notify_server_event(g.server["id"], "tell", uuids=[duel["opponent_uuid"]],
                             text=f"&7{duel['challenger']} hat die Herausforderung zum Duell zurückgezogen.")
    return ("", 200)


@server_bp.route("/api/duels/respond", methods=["POST"])
@player_required
def duel_respond_api():
    data = request.get_json(silent=True) or {}
    try:
        duel_id = int(data.get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Duell."}, 400
    accept = bool(data.get("accept"))
    duel = db().respond_duel(duel_id, logged_in_player_id(), accept)
    if duel is None:
        return {"error": "Diese Herausforderung gibt es nicht mehr."}, 404
    metric = metrics_mod.METRICS_BY_KEY[duel["metric"]]
    if accept:
        db().notify_server_event(g.server["id"], "broadcast", color="gold",
                                 text=f"⚔ Duell: {duel['challenger']} gegen {duel['opponent']} – wer schafft in "
                                      f"{duel['days']} {'Tag' if duel['days'] == 1 else 'Tagen'} mehr {metric.label}?")
    else:
        db().notify_server_event(g.server["id"], "tell", uuids=[duel["challenger_uuid"]],
                                 text=f"&7{duel['opponent']} hat dein Duell abgelehnt.")
    return ("", 200)


REPORT_REASON_MAX = 300


@server_bp.route("/api/reports", methods=["POST"])
@player_required
def report_create_api():
    data = request.get_json(silent=True) or {}
    reason = " ".join(str(data.get("reason") or "").split())
    if not 5 <= len(reason) <= REPORT_REASON_MAX:
        return {"error": f"Beschreibe kurz, was passiert ist (5-{REPORT_REASON_MAX} Zeichen)."}, 400
    target = str(data.get("name") or "").strip() or None
    if target:
        target_id = db().get_player_id_from_player_name_and_server_id(target, g.server["id"])
        if target_id is None:
            return {"error": "Diesen Spieler gibt es auf dem Server nicht."}, 404
        target = db().get_player_name_from_player_id(target_id)
    player_id = logged_in_player_id()
    if db().create_report(g.server["id"], player_id, target, reason, source="web") is None:
        return {"error": "Du hast in der letzten Stunde schon genug gemeldet."}, 429
    reporter = db().get_player_name_from_player_id(player_id)
    moderators = db().get_online_moderator_uuids(g.server["id"])
    if moderators:
        db().notify_server_event(g.server["id"], "tell", uuids=moderators,
                                 text=f"&c[Meldung] &f{reporter}{' meldet ' + target if target else ' hat etwas gemeldet'} "
                                      "&7(Website) – siehe Verwaltung")
    return ("", 200)


@server_bp.route("/api/mod/reports/resolve", methods=["POST"])
@moderator_required
def mod_report_resolve_api():
    try:
        report_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannte Meldung."}, 400
    actor = db().get_player_name_from_player_id(logged_in_player_id())
    resolved = db().resolve_report(g.server["id"], report_id, actor)
    if resolved is None:
        return {"error": "Unbekannte Meldung."}, 404
    db().add_mod_log(g.server["id"], actor, "report_resolve", resolved[0], resolved[1][:200])
    return ("", 200)


@server_bp.route("/api/favorites/toggle", methods=["POST"])
@player_required
def favorite_toggle_api():
    result = db().toggle_favorite(logged_in_player_id(), str((request.get_json(silent=True) or {}).get("name") or ""))
    if result is None:
        return {"error": "Unbekannter Spieler."}, 404
    return {"favorite": result}


@server_bp.route("/api/prefix/save", methods=["POST"])
@player_required
def prefix_save_api():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    color = str(data.get("color") or "")
    password = str(data.get("password") or "")
    if not PREFIX_TEXT_RE.match(text):
        return {"error": "Der Prefix muss 1-16 Zeichen lang sein (Buchstaben, Zahlen, Leerzeichen und _ . ! ? + * # -)."}, 400
    if color not in PREFIX_COLORS:
        return {"error": "Unbekannte Farbe."}, 400
    if color in MODERATOR_PREFIX_COLORS and not db().is_moderator(logged_in_player_id()):
        return {"error": "Diese Farbe ist Moderatoren vorbehalten."}, 403
    if password and len(password) < 4:
        return {"error": "Das Passwort muss mindestens 4 Zeichen lang sein."}, 400
    try:
        uuids = db().save_own_prefix(logged_in_player_id(), text, color, password=password or None,
                                     remove_password=bool(data.get("remove_password")))
    except psycopg2.errors.UniqueViolation:
        return {"error": "Diesen Prefix gibt es auf dem Server schon."}, 409
    db().notify_server_event(g.server["id"], "prefix", uuids=uuids)
    return ("", 200)


@server_bp.route("/api/prefix/delete", methods=["POST"])
@player_required
def prefix_delete_api():
    uuids = db().delete_own_prefix(logged_in_player_id())
    db().notify_server_event(g.server["id"], "prefix", uuids=uuids)
    return ("", 200)


@server_bp.route("/api/prefix/join", methods=["POST"])
@player_required
def prefix_join_api():
    data = request.get_json(silent=True) or {}
    try:
        prefix_id = int(data.get("prefix_id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannter Prefix."}, 400
    result = db().join_prefix(logged_in_player_id(), prefix_id, str(data.get("password") or ""))
    if result == "wrong password":
        return {"error": "Falsches Passwort."}, 403
    if result != "ok":
        return {"error": "Unbekannter Prefix."}, 404
    db().notify_server_event(g.server["id"], "prefix", uuids=[session.get("uuid")])
    return ("", 200)


@server_bp.route("/api/prefix/leave", methods=["POST"])
@player_required
def prefix_leave_api():
    db().leave_prefix(logged_in_player_id())
    db().notify_server_event(g.server["id"], "prefix", uuids=[session.get("uuid")])
    return ("", 200)


@server_bp.route("/api/mod/bans")
@moderator_required
def mod_bans_api():
    return {"bans": bans_json(g.server["id"])}


@server_bp.route("/api/mod/ban", methods=["POST"])
@moderator_required
def mod_ban_api():
    return do_ban(g.server["id"], request.get_json(silent=True) or {},
                  banned_by=db().get_player_name_from_player_id(logged_in_player_id()))


@server_bp.route("/api/mod/unban", methods=["POST"])
@moderator_required
def mod_unban_api():
    return do_unban(g.server["id"], request.get_json(silent=True) or {},
                    unbanned_by=db().get_player_name_from_player_id(logged_in_player_id()))


COMPETITION_TITLE_RE = re.compile(r"^[A-Za-z0-9ÄÖÜäöüß _.,:!?+*#()'-]{3,60}$")
MAX_COMPETITION_DAYS = 90


@server_bp.route("/api/mod/competitions", methods=["POST"])
@moderator_required
def mod_competition_create_api():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    metric = str(data.get("metric") or "")
    if not COMPETITION_TITLE_RE.match(title):
        return {"error": "Der Titel muss 3-60 Zeichen lang sein (Buchstaben, Zahlen, Leerzeichen und einfache Satzzeichen)."}, 400
    if metric not in metrics_mod.METRICS_BY_KEY:
        return {"error": "Unbekannte Kennzahl."}, 400
    try:
        starts_on = date.fromisoformat(str(data.get("starts_on")))
        ends_on = date.fromisoformat(str(data.get("ends_on")))
    except ValueError:
        return {"error": "Ungültiges Datum."}, 400
    today = db().get_today()
    if starts_on < today:
        return {"error": "Der Wettbewerb kann frühestens heute beginnen."}, 400
    if ends_on < starts_on or (ends_on - starts_on).days >= MAX_COMPETITION_DAYS:
        return {"error": f"Das Ende muss nach dem Start liegen, höchstens {MAX_COMPETITION_DAYS} Tage."}, 400
    actor = db().get_player_name_from_player_id(logged_in_player_id())
    competition_id = db().create_competition(g.server["id"], title, metric, starts_on, ends_on, created_by=actor)
    db().add_mod_log(g.server["id"], actor, "competition_create", None,
                     f"{title} · {metrics_mod.METRICS_BY_KEY[metric].label} · "
                     f"{starts_on.strftime('%d.%m.')}–{ends_on.strftime('%d.%m.%Y')}")
    return {"id": competition_id}, 200


@server_bp.route("/api/mod/competitions/delete", methods=["POST"])
@moderator_required
def mod_competition_delete_api():
    try:
        competition_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannter Wettbewerb."}, 400
    competition = db().get_competition(competition_id)
    if not db().delete_competition(g.server["id"], competition_id):
        return {"error": "Unbekannter Wettbewerb."}, 404
    db().add_mod_log(g.server["id"], db().get_player_name_from_player_id(logged_in_player_id()),
                     "competition_delete", None, competition["title"])
    return ("", 200)


EVENT_TITLE_MAX, EVENT_TEXT_MAX, EVENT_PLACE_MAX = 60, 500, 80
POLL_QUESTION_MAX, POLL_OPTION_MAX, POLL_MAX_DAYS = 120, 60, 60


def mod_name():
    return db().get_player_name_from_player_id(logged_in_player_id())


def json_id(message):
    """The "id" of the JSON body as int, or raises ValueError."""
    try:
        return int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        raise ValueError(message)


@server_bp.route("/api/mod/events", methods=["POST"])
@moderator_required
def mod_event_create_api():
    data = request.get_json(silent=True) or {}
    title = " ".join(str(data.get("title") or "").split())
    description = str(data.get("description") or "").strip() or None
    place = " ".join(str(data.get("place") or "").split()) or None
    if not 3 <= len(title) <= EVENT_TITLE_MAX:
        return {"error": f"Der Titel muss 3-{EVENT_TITLE_MAX} Zeichen lang sein."}, 400
    if description and len(description) > EVENT_TEXT_MAX or place and len(place) > EVENT_PLACE_MAX:
        return {"error": "Beschreibung oder Treffpunkt sind zu lang."}, 400
    try:
        starts_at = datetime.fromisoformat(str(data.get("starts_at"))).replace(tzinfo=ZoneInfo(config.TIMEZONE))
    except ValueError:
        return {"error": "Ungültiger Zeitpunkt."}, 400
    if starts_at < datetime.now(timezone.utc) or starts_at > datetime.now(timezone.utc) + timedelta(days=365):
        return {"error": "Das Event muss in der Zukunft liegen (höchstens ein Jahr)."}, 400
    event_id = db().create_event(g.server["id"], title, starts_at, description, place, created_by=mod_name())
    db().add_mod_log(g.server["id"], mod_name(), "event_create", None, f"{title} · {starts_at.strftime('%d.%m.%Y %H:%M')}")
    # announce it right away; an event that starts within the reminder time gets its reminder now
    event = db().get_event(event_id)
    soon = db().mark_event_reminded_if_soon(event_id)
    color, text = game_commands.event_announcement("reminder" if soon else "new", event)
    db().notify_server_event(g.server["id"], "broadcast", color=color, text=text)
    return {"id": event_id}, 200


@server_bp.route("/api/mod/events/delete", methods=["POST"])
@moderator_required
def mod_event_delete_api():
    try:
        title = db().delete_event(g.server["id"], json_id("Unbekanntes Event."))
    except ValueError as e:
        return {"error": str(e)}, 400
    if title is None:
        return {"error": "Unbekanntes Event."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "event_delete", None, title)
    return ("", 200)


@server_bp.route("/api/mod/polls", methods=["POST"])
@moderator_required
def mod_poll_create_api():
    data = request.get_json(silent=True) or {}
    question = " ".join(str(data.get("question") or "").split())
    options = [" ".join(str(o).split()) for o in (data.get("options") or []) if str(o).strip()]
    if not 5 <= len(question) <= POLL_QUESTION_MAX:
        return {"error": f"Die Frage muss 5-{POLL_QUESTION_MAX} Zeichen lang sein."}, 400
    if not 2 <= len(options) <= 8 or any(len(o) > POLL_OPTION_MAX for o in options) or \
            len({o.lower() for o in options}) != len(options):
        return {"error": f"2 bis 8 verschiedene Antworten mit höchstens {POLL_OPTION_MAX} Zeichen."}, 400
    try:
        days = int(data.get("days"))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= POLL_MAX_DAYS:
        return {"error": f"Eine Umfrage läuft 1 bis {POLL_MAX_DAYS} Tage."}, 400
    poll_id = db().create_poll(g.server["id"], question, options, datetime.now(timezone.utc) + timedelta(days=days),
                               created_by=mod_name())
    db().add_mod_log(g.server["id"], mod_name(), "poll_create", None, question)
    db().notify_server_event(g.server["id"], "broadcast", color="gold",
                             text=f"★ Neue Umfrage: {game_commands.clean(question)} "
                                  + game_commands.button("&a[Abstimmen]", "/vote"))
    return {"id": poll_id}, 200


@server_bp.route("/api/mod/polls/delete", methods=["POST"])
@moderator_required
def mod_poll_delete_api():
    try:
        question = db().delete_poll(g.server["id"], json_id("Unbekannte Umfrage."))
    except ValueError as e:
        return {"error": str(e)}, 400
    if question is None:
        return {"error": "Unbekannte Umfrage."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "poll_delete", None, question)
    return ("", 200)


@server_bp.route("/api/mod/builds/approve", methods=["POST"])
@moderator_required
def mod_build_approve_api():
    try:
        build = db().approve_build(g.server["id"], json_id("Unbekanntes Bild."), mod_name())
    except ValueError as e:
        return {"error": str(e)}, 400
    if build is None:
        return {"error": "Unbekanntes Bild."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "build_approve", build["name"], build["title"])
    db().notify_server_event(g.server["id"], "tell", uuids=[build["uuid"]],
                             text=f"&a[Galerie] Dein Bild »{game_commands.clean(build['title'])}« ist jetzt in der Galerie zu sehen!")
    return ("", 200)


@server_bp.route("/api/mod/builds/delete", methods=["POST"])
@moderator_required
def mod_build_delete_api():
    try:
        deleted = db().delete_build(g.server["id"], json_id("Unbekanntes Bild."))
    except ValueError as e:
        return {"error": str(e)}, 400
    if deleted is None:
        return {"error": "Unbekanntes Bild."}, 404
    delete_images([deleted[1]], config.UPLOAD_DIR)
    db().add_mod_log(g.server["id"], mod_name(), "build_delete", deleted[2], deleted[0])
    return ("", 200)


@server_bp.route("/api/mod/guestbook/keep", methods=["POST"])
@moderator_required
def mod_guestbook_keep_api():
    try:
        entry_id = json_id("Unbekannter Eintrag.")
    except ValueError as e:
        return {"error": str(e)}, 400
    if not db().keep_guestbook_entry(g.server["id"], entry_id):
        return {"error": "Unbekannter Eintrag."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "guestbook_keep", None, f"Eintrag {entry_id}")
    return ("", 200)


ACCESS_MODES = ("off", "application", "code", "both")
RULES_MAX_LENGTH = 10_000


@server_bp.route("/api/mod/settings", methods=["POST"])
@moderator_required
def mod_settings_api():
    data = request.get_json(silent=True) or {}
    fields = {}
    if "access_mode" in data:
        if data["access_mode"] not in ACCESS_MODES:
            return {"error": "Unbekannter Zugang."}, 400
        fields["access_mode"] = data["access_mode"]
    for key, low, high in (("warn_threshold", 0, 20), ("warn_ban_days", 0, 3650)):
        if key in data:
            try:
                fields[key] = int(data[key])
            except (TypeError, ValueError):
                return {"error": "Ungültige Zahl."}, 400
            if not low <= fields[key] <= high:
                return {"error": f"Die Zahl muss zwischen {low} und {high} liegen."}, 400
    if "rules_enabled" in data:
        fields["rules_enabled"] = bool(data["rules_enabled"])
    for key in ("rules", "faq"):
        if key in data:
            text = str(data[key] or "").replace("\r\n", "\n").strip()
            if len(text) > RULES_MAX_LENGTH:
                return {"error": f"Höchstens {RULES_MAX_LENGTH} Zeichen."}, 400
            fields[key] = text or None
    db().update_server_settings(g.server["id"], **fields)
    db().add_mod_log(g.server["id"], mod_name(), "settings", None, ", ".join(sorted(fields)))
    if "access_mode" in fields:
        db().notify_server_event(g.server["id"], "joininfo")
    return ("", 200)


@server_bp.route("/api/mod/applications", methods=["POST"])
@moderator_required
def mod_application_api():
    data = request.get_json(silent=True) or {}
    try:
        request_id = int(data.get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannte Bewerbung."}, 400
    accept = bool(data.get("accept"))
    handled = db().handle_application(g.server["id"], request_id, accept, mod_name())
    if handled is None:
        return {"error": "Unbekannte Bewerbung."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "application_accept" if accept else "application_reject", handled["name"])
    if accept:
        db().notify_server_event(g.server["id"], "whitelist")
    return ("", 200)


@server_bp.route("/api/mod/codes", methods=["POST"])
@moderator_required
def mod_code_create_api():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip().upper() or secrets.token_hex(4).upper()
    if not re.match(r"^[A-Z0-9-]{4,24}$", code):
        return {"error": "Der Code muss 4-24 Zeichen lang sein (Buchstaben, Zahlen, -)."}, 400
    try:
        max_uses = int(data["max_uses"]) if data.get("max_uses") not in (None, "") else None
        days = int(data["days"]) if data.get("days") not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "Ungültige Zahl."}, 400
    if max_uses is not None and not 1 <= max_uses <= 1000 or days is not None and not 1 <= days <= 365:
        return {"error": "Nutzungen 1-1000, Gültigkeit 1-365 Tage."}, 400
    expires = datetime.now(timezone.utc) + timedelta(days=days) if days else None
    if db().create_invite_code(g.server["id"], code, max_uses, expires, mod_name()) is None:
        return {"error": "Diesen Code gibt es schon."}, 409
    db().add_mod_log(g.server["id"], mod_name(), "code_create", None, code)
    return {"code": code}, 200


@server_bp.route("/api/mod/codes/delete", methods=["POST"])
@moderator_required
def mod_code_delete_api():
    try:
        code = db().delete_invite_code(g.server["id"], json_id("Unbekannter Code."))
    except ValueError as e:
        return {"error": str(e)}, 400
    if code is None:
        return {"error": "Unbekannter Code."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "code_delete", None, code)
    return ("", 200)


def web_tell(server_id):
    return lambda uuid, text: db().notify_server_event(server_id, "tell", uuids=[uuid], text=text)


def web_ban(server_id):
    def ban(result):
        end_ms = int(result["end"].timestamp() * 1000) if result["end"] else 0
        db().notify_server_event(server_id, "ban", uuid=result["uuid"], name=result["name"], reason=result["reason"],
                                 end_ms=end_ms)
    return ban


def web_mute(server_id):
    return lambda uuid, until, reason: db().notify_server_event(
        server_id, "mute", uuid=uuid, until_ms=int(until.timestamp() * 1000) if until else 0, reason=reason)


@server_bp.route("/api/mod/warn", methods=["POST"])
@moderator_required
def mod_warn_api():
    data = request.get_json(silent=True) or {}
    reason = " ".join(str(data.get("reason") or "").split())[:game_commands.WARN_REASON_MAX]
    player_id = db().get_player_id_from_player_name_and_server_id(str(data.get("name") or ""), g.server["id"])
    if player_id is None:
        return {"error": "Unbekannter Spieler."}, 404
    if len(reason) < 3:
        return {"error": "Bitte einen Grund angeben."}, 400
    text = game_commands.apply_warning(db(), g.server["id"], player_id, reason, mod_name(),
                                       web_tell(g.server["id"]), web_ban(g.server["id"]))
    return {"message": text}, 200


@server_bp.route("/api/mod/warnings/delete", methods=["POST"])
@moderator_required
def mod_warning_delete_api():
    try:
        deleted = db().delete_warning(g.server["id"], json_id("Unbekannte Verwarnung."))
    except ValueError as e:
        return {"error": str(e)}, 400
    if deleted is None:
        return {"error": "Unbekannte Verwarnung."}, 404
    db().add_mod_log(g.server["id"], mod_name(), "warning_delete", deleted[0], deleted[1][:200])
    return ("", 200)


@server_bp.route("/api/mod/mute", methods=["POST"])
@moderator_required
def mod_mute_api():
    data = request.get_json(silent=True) or {}
    player_id = db().get_player_id_from_player_name_and_server_id(str(data.get("name") or ""), g.server["id"])
    if player_id is None:
        return {"error": "Unbekannter Spieler."}, 404
    try:
        minutes = int(data.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = -1
    if minutes == 0:
        game_commands.unmute_player(db(), g.server["id"], player_id, mod_name(), web_mute(g.server["id"]),
                                    web_tell(g.server["id"]))
        return {"message": "Stummschaltung aufgehoben."}, 200
    if not 1 <= minutes <= game_commands.MUTE_MAX_MINUTES:
        return {"error": "Ungültige Dauer."}, 400
    reason = " ".join(str(data.get("reason") or "").split())[:game_commands.WARN_REASON_MAX] or None
    until = game_commands.mute_player(db(), g.server["id"], player_id, minutes, reason, mod_name(),
                                      web_mute(g.server["id"]), web_tell(g.server["id"]))
    return {"message": f"Stummgeschaltet {game_commands.mute_text(until)}."}, 200


MAX_GOAL_DAYS = 365


@server_bp.route("/api/mod/goals", methods=["POST"])
@moderator_required
def mod_goal_create_api():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    metric = metrics_mod.METRICS_BY_KEY.get(str(data.get("metric") or ""))
    if not COMPETITION_TITLE_RE.match(title):
        return {"error": "Der Titel muss 3-60 Zeichen lang sein (Buchstaben, Zahlen, Leerzeichen und einfache Satzzeichen)."}, 400
    if metric is None:
        return {"error": "Unbekannte Kennzahl."}, 400
    try:
        amount = float(str(data.get("amount") or "").replace(",", "."))
        starts_on = date.fromisoformat(str(data.get("starts_on")))
        ends_on = date.fromisoformat(str(data["ends_on"])) if data.get("ends_on") else None
    except ValueError:
        return {"error": "Ungültige Angaben."}, 400
    target = motivation.goal_target(metric, amount) if amount == amount else 0  # NaN
    if not 0 < target < 10 ** 15:
        return {"error": "Das Ziel muss größer als 0 sein."}, 400
    today = db().get_today()
    if starts_on < today:
        return {"error": "Das Ziel kann frühestens heute beginnen."}, 400
    if ends_on and (ends_on < starts_on or (ends_on - starts_on).days >= MAX_GOAL_DAYS):
        return {"error": f"Das Ende muss nach dem Start liegen, höchstens {MAX_GOAL_DAYS} Tage."}, 400
    actor = db().get_player_name_from_player_id(logged_in_player_id())
    goal_id = db().create_goal(g.server["id"], title, metric.key, target, starts_on, ends_on, created_by=actor)
    db().add_mod_log(g.server["id"], actor, "goal_create", None,
                     f"{title} · {motivation.format_goal_value(metric, target)} {metric.label}")
    return {"id": goal_id}, 200


@server_bp.route("/api/mod/goals/delete", methods=["POST"])
@moderator_required
def mod_goal_delete_api():
    try:
        goal_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekanntes Ziel."}, 400
    title = db().delete_goal(g.server["id"], goal_id)
    if title is None:
        return {"error": "Unbekanntes Ziel."}, 404
    db().add_mod_log(g.server["id"], db().get_player_name_from_player_id(logged_in_player_id()), "goal_delete", None, title)
    return ("", 200)


NOTE_MAX_LENGTH = 1000


@server_bp.route("/api/mod/notes", methods=["POST"])
@moderator_required
def mod_note_add_api():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text or len(text) > NOTE_MAX_LENGTH:
        return {"error": f"Die Notiz muss 1-{NOTE_MAX_LENGTH} Zeichen lang sein."}, 400
    player_id = db().get_player_id_from_player_name_and_server_id(str(data.get("name") or ""), g.server["id"])
    if player_id is None:
        return {"error": "Unbekannter Spieler."}, 404
    author = db().get_player_name_from_player_id(logged_in_player_id())
    db().add_player_note(player_id, author, text)
    db().add_mod_log(g.server["id"], author, "note_add", db().get_player_name_from_player_id(player_id), text[:200])
    return ("", 200)


@server_bp.route("/api/mod/notes/delete", methods=["POST"])
@moderator_required
def mod_note_delete_api():
    try:
        note_id = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return {"error": "Unbekannte Notiz."}, 400
    deleted = db().delete_player_note(g.server["id"], note_id)
    if deleted is None:
        return {"error": "Unbekannte Notiz."}, 404
    db().add_mod_log(g.server["id"], db().get_player_name_from_player_id(logged_in_player_id()), "note_delete",
                     deleted[0], deleted[1][:200])
    return ("", 200)


################################ BANS (shared by moderators and server admins) #################################

def bans_json(server_id):
    fmt = lambda ts: ts.strftime("%d.%m.%Y %H:%M") if ts else None
    return [{"player_id": b["player_id"], "name": b["name"], "uuid": b["uuid"], "source": b["source"],
             "reason": b["reason"], "banned_by": b["banned_by"], "comment": b["comment"],
             "start": fmt(b["start"]), "end": fmt(b["end"])} for b in db().list_active_bans(server_id)]


BAN_TEXT_MAX = 100


def do_ban(server_id, data, banned_by):
    name = str(data.get("name") or "").strip()
    reason_id = data.get("reason_id")
    days = data.get("days")
    try:
        reason_id = int(reason_id) if reason_id not in (None, "") else None
        days = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "Ungültige Angaben."}, 400
    if days is not None and not 0 <= days <= 3650:
        return {"error": "Die Dauer muss zwischen 0 (dauerhaft) und 3650 Tagen liegen."}, 400
    comment = str(data.get("comment") or "").strip()[:500] or None
    reason_text = game_commands.clean(" ".join(str(data.get("reason_text") or "").split()))[:BAN_TEXT_MAX] or None
    ban = db().ban_player(server_id, name, banned_by, reason_id=reason_id, days=days, comment=comment,
                          reason_text=reason_text)
    if ban is None:
        return {"error": "Diesen Spieler gibt es auf dem Server nicht."}, 404
    end_ms = int(ban["end"].timestamp() * 1000) if ban["end"] else 0
    db().notify_server_event(server_id, "ban", uuid=ban["uuid"], name=ban["name"], reason=ban["reason"], end_ms=end_ms)
    until = ban["end"].strftime("bis %d.%m.%Y %H:%M") if ban["end"] else "dauerhaft"
    db().add_mod_log(server_id, banned_by, "ban", ban["name"],
                     " · ".join(part for part in (ban["reason"], until, comment) if part))
    return ("", 200)


def do_unban(server_id, data, unbanned_by=None):
    try:
        player_id = str(uuid_mod.UUID(str(data.get("player_id"))))
    except ValueError:
        return {"error": "Unbekannter Spieler."}, 400
    result = db().unban_player(server_id, player_id)
    if result is None:
        return {"error": "Unbekannter Spieler."}, 404
    db().notify_server_event(server_id, "unban", uuid=result["uuid"], name=result["name"])
    db().add_mod_log(server_id, unbanned_by, "unban", result["name"])
    return ("", 200)


################################ SERVER API #################################

def _login_error(response, info, status="error"):
    return {"response": response, "status": status, "info": info}


@server_bp.route("/api/login", methods=["POST"])
def minecraft_login_api():
    """
    Two step player login:
    1. {"username": name, "pin": null} -> a pin is sent to the player in-game
    2. {"username": null, "pin": "123456"} -> the pin is checked
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"response": "Invalid content"}, 400
    username, pin = data.get("username"), data.get("pin")

    if pin is None:
        player_id = db().get_player_id_from_player_name_and_server_id(username or "", g.server["id"])
        if player_id is None:
            return _login_error("Invalid username", "Dieser Spieler war noch nie auf diesem Server.")
        if not db().get_online_status_by_player_id(player_id):
            return _login_error("You are offline", "Du musst auf dem Server online sein, um dich einzuloggen.")
        if not db().is_plugin_online(g.server["id"]):
            return _login_error("Server not connected",
                                "Der Minecraft-Server ist gerade nicht mit MCConnect verbunden. Versuche es später erneut.")
        # every request is a new pin with new attempts: limit them against guessing (and chat spam)
        if not rate_limit("login_pin", str(player_id)) or not rate_limit("login_pin_client", request.remote_addr):
            return _login_error("Too many requests", "Zu viele Anfragen. Warte ein paar Minuten und versuche es erneut.")
        db().add_login_entry_from_player_id(player_id, secrets.randbelow(900000) + 100000)
        session["login_player_id"] = str(player_id)
        session["login_server_id"] = g.server["id"]
        session["login_name"] = db().get_player_name_from_player_id(player_id)
        return {"response": "success", "status": "success", "info": ""}

    player_id = session.get("login_player_id")
    if player_id is None or session.get("login_server_id") != g.server["id"]:
        session.clear()
        return _login_error("Cookie is wrong", "Etwas ist mit deinen Cookies schiefgelaufen. Bitte versuche es erneut.",
                            "reset")
    try:
        pin = int(pin)
    except (TypeError, ValueError):
        return _login_error("Pin is incorrect", "Die Pin ist falsch. Bitte versuche es erneut.")

    result = db().verify_player_login(player_id, pin)
    if result[0]:
        session.clear()
        session["player_id"] = player_id
        session["server_id"] = g.server["id"]
        session["uuid"] = str(db().get_mojang_uuid_from_player_id(player_id))
        session.permanent = True
        return {"response": "Pin is correct", "status": "success", "info": ""}

    reason = result[1]
    if reason == "wrong pin provided":
        return _login_error("Pin is incorrect", "Die Pin ist falsch. Bitte versuche es erneut.")
    session.pop("login_player_id", None)
    if reason == "timeout reached":
        return _login_error("Timed out", "Deine Pin ist abgelaufen (5 Minuten gültig). Bitte versuche es erneut.",
                            "reset")
    if reason == "too many attempts":
        return _login_error("Too many attempts", "Zu viele falsche Versuche. Bitte fordere eine neue Pin an.", "reset")
    return _login_error("No login pending", "Bitte starte den Login erneut.", "reset")


@server_bp.route("/api/player_count")
def live_player_count():
    return live_json(("count", g.subdomain), lambda: db().get_online_player_count_from_subdomain(g.subdomain))


@server_bp.route("/api/status")
def live_status():
    """Online status of all players, in the same order as the player list."""
    return live_json(("status", g.subdomain), lambda: ["online" if p["online"] else "offline"
                                                       for p in db().get_players_overview_from_subdomain(g.subdomain)])


def _custom_stat(database, player_id, *names):
    """First existing value of the given custom stats (names differ between game versions)."""
    for name in names:
        value = database.get_value_from_unique_object_from_action_table_with_player_id(name, player_id)
        if value is not None:
            return value
    return 0


@server_bp.route("/api/player_info/<path:player_name>")
def live_player_info(player_name):
    database = db()
    player_id = database.get_player_id_from_player_name_and_server_id(player_name, g.server["id"])
    if player_id is None or (database.is_stats_hidden(player_id) and str(logged_in_player_id()) != str(player_id)):
        abort(404)

    def player_info():
        info = database.get_player_info_by_player_id(player_id)
        fmt = lambda ts: ts.strftime("%d.%m.%Y, %H:%M Uhr") if ts else "-"
        deaths = _custom_stat(database, player_id, "minecraft:deaths")
        # Without a death the game counts time_since_death from the first join, which would be misleading.
        since_death = format_time(_custom_stat(database, player_id, "minecraft:time_since_death") / 20) if deaths else "-"
        return [
            str(info["mojang_uuid"]),
            "online" if info["online"] else "offline",
            deaths,
            fmt(info["first_seen"]),
            fmt(info["last_seen"]),
            since_death,
            format_time(_custom_stat(database, player_id, "minecraft:play_time", "minecraft:play_one_minute") / 20),
        ]
    return live_json(("player", str(player_id)), player_info)




################################ LEGAL PAGES (all domains) #################################

def render_legal(page):
    return render_template(f"legal_{page}.html", legal={
        "name": config.LEGAL_NAME, "address": config.LEGAL_ADDRESS,
        "email": config.LEGAL_EMAIL, "phone": config.LEGAL_PHONE,
    })


@main_bp.route("/impressum")
@server_bp.route("/impressum")
def impressum():
    return render_legal("impressum")


@main_bp.route("/datenschutz")
@server_bp.route("/datenschutz")
def datenschutz():
    return render_legal("datenschutz")


################################ MAIN DOMAIN #################################

def current_admin_id():
    """
    The logged in admin, or None. A session is only valid if it started after the
    last password change (and the admin still exists); otherwise it is cleared.
    """
    admin_id = session.get("admin_id")
    if not admin_id:
        return None
    if "admin_valid" in g:
        return admin_id if g.admin_valid else None
    changed_at = db().get_admin_password_changed_at(admin_id)
    g.admin_valid = changed_at is not None and session.get("admin_login_at", 0) >= changed_at
    if not g.admin_valid:
        session.clear()
        return None
    return admin_id


@main_bp.context_processor
def inject_main_context():
    return {"admin_username": session.get("admin_username") if current_admin_id() else None}


def admin_required(view=None, *, allow_upload=False):
    """
    Pages redirect to /login, API calls get 401. POST API calls must be JSON, which
    browsers cannot send cross-site without CORS. Upload endpoints (allow_upload)
    accept multipart forms instead and check the Origin header.
    """
    if view is None:
        return functools.partial(admin_required, allow_upload=allow_upload)

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        is_api = request.path.startswith("/api/")
        if not current_admin_id():
            return ({"error": "not logged in"}, 401) if is_api else redirect("/login")
        if is_api and request.method == "POST":
            if allow_upload and request.mimetype == "multipart/form-data":
                origin = request.headers.get("Origin")
                if origin and urlparse(origin).netloc != request.host:
                    return {"error": "cross-site upload refused"}, 403
            elif not request.is_json:
                return {"error": "json required"}, 415
        return view(*args, **kwargs)
    return wrapper


@main_bp.route("/")
def main_index_route():
    servers = db().get_directory()
    for server in servers:
        server["url"] = f"{config.PUBLIC_SCHEME}://{server['subdomain']}.{current_app.config['SERVER_NAME']}/"
        server["banner_url"] = image_url(server["banner"]) if server["banner"] else None
    return render_template("index-main.html", servers=servers)


@main_bp.route("/login")
def server_admin_login():
    if current_admin_id():
        return redirect("/manage")
    return render_template("serverAdminLogin.html")


@main_bp.route("/create")
@admin_required
def create_new_server():
    return render_template("serverAdminCreate.html", base_domain=current_app.config["SERVER_NAME"])


@main_bp.route("/manage")
@admin_required
def manage_server():
    servers = db().get_servers_by_owner(session["admin_id"])
    for server in servers:
        server["health"] = admin_health(server)
        server["images"] = db().get_server_images(server["id"])
        server["players"] = db().get_players_overview_from_subdomain(server["subdomain"])
    return render_template("serverAdminManage.html",
                           servers=servers, max_gallery_images=MAX_GALLERY_IMAGES,
                           ban_reasons=db().get_ban_reasons(),
                           base_domain=current_app.config["SERVER_NAME"],
                           plugin_host=config.PLUGIN_PUBLIC_HOST, plugin_port=config.PLUGIN_PUBLIC_PORT,
                           plugin_tls_port=config.PLUGIN_PUBLIC_TLS_PORT,
                           plugin_available=plugin_jar_path() is not None)


@main_bp.route("/healthz")
def healthz():
    db()._fetchvalue("SELECT 1")
    return {"status": "ok"}


@main_bp.route("/api/player_count")
def live_total_player_count():
    return live_json(("count",), db().get_online_player_count_total)


def admin_health(server):
    """Short health summary for the admin page (None before the first sample)."""
    latest = db().get_latest_health(server["id"])
    if latest is None:
        return None
    stale = latest["at"] < datetime.now(timezone.utc) - timedelta(minutes=10)
    tps = latest["tps"]
    availability = db().get_health_availability(server["id"])
    return {"stale": stale, "sampled": latest["at"].strftime("%d.%m.%Y, %H:%M Uhr"),
            "tps": None if tps is None else f"{tps:.1f}".replace(".", ","),
            "status": None if tps is None or stale else "good" if tps >= TPS_GOOD else "warning" if tps >= TPS_WARNING else "critical",
            "memory": f"{latest['mem_used_mb']} / {latest['mem_max_mb']} MB" if latest["mem_used_mb"] is not None else "–",
            "players": latest["players"], "version": latest["mc_version"],
            "uptime": format_duration(latest["uptime_s"]) if latest["uptime_s"] is not None else "–",
            "availability": None if availability is None else f"{availability * 100:.1f} %".replace(".", ",")}


def plugin_jar_path():
    if config.PLUGIN_JAR:
        return config.PLUGIN_JAR if os.path.isfile(config.PLUGIN_JAR) else None
    builds = sorted(glob.glob(os.path.join(PROJECT_ROOT, "java plugin", "MCDataLink", "target", "MCDataLink-*.jar")))
    return builds[-1] if builds else None


@main_bp.route("/download/MCDataLink.jar")
def download_plugin():
    path = plugin_jar_path()
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name="MCDataLink.jar")


################################ SERVER ADMIN API #################################

@main_bp.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not USERNAME_RE.match(username):
        return {"error": "Der Benutzername muss 3-32 Zeichen lang sein (Buchstaben, Zahlen, _ . -)."}, 400
    if not EMAIL_RE.match(email):
        return {"error": "Die E-Mail-Adresse ist ungültig."}, 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return {"error": f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein."}, 400

    try:
        admin_id = db().add_server_admin(username, password, email, replace_unverified=True)
    except psycopg2.errors.UniqueViolation:
        return {"error": "Benutzername oder E-Mail-Adresse ist bereits vergeben."}, 409
    send_verification_email(username, email, db().create_email_verification(admin_id))
    return ("", 200)


@main_bp.route("/api/resend_verification", methods=["POST"])
def resend_verification():
    """Always answers the same, so it cannot be used to find out which addresses are registered."""
    email = str((request.get_json(silent=True) or {}).get("email") or "").strip()
    if EMAIL_RE.match(email):
        renewed = db().renew_email_verification(email)
        if renewed:
            send_verification_email(renewed[0], email, renewed[1])
    return ("", 200)


def send_mail(recipient, subject, html, log_hint):
    """Send via SMTP, or log log_hint (containing the link) when no SMTP is configured."""
    mailer = current_app.extensions.get("mcconnect_mailer")
    if mailer:
        mailer.send_email(recipient, subject, html)
    else:
        logger.warning(f"No SMTP configured, {log_hint}")


def send_verification_email(username, email, token):
    link = f"{config.PUBLIC_SCHEME}://{current_app.config['SERVER_NAME']}/verify_email/{username}/{token}"
    send_mail(email, "MCConnect: E-Mail bestätigen",
              f'<p>Hallo {username},</p><p>bitte bestätige deine E-Mail-Adresse: '
              f'<a href="{link}">{link}</a></p><p>Der Link ist 24 Stunden gültig.</p>',
              log_hint=f"verification link for {username}: {link}")


@main_bp.route("/api/login", methods=["POST"])
def server_login_api():
    data = request.get_json(silent=True) or {}
    login = str(data.get("username") or "").strip()
    if not rate_limit("admin_login", request.remote_addr) or not rate_limit("admin_login_name", login.lower()):
        return {"error": "Zu viele Versuche. Warte ein paar Minuten."}, 429
    admin = db().authenticate_admin(login, str(data.get("password") or ""))
    if admin:
        session.clear()
        session["admin_id"], session["admin_username"] = admin
        session["admin_login_at"] = time.time()
        session.permanent = True
        return ("", 200)
    return ("", 400)


@main_bp.route("/forgot_password")
def forgot_password():
    return render_template("forgot_password.html")


@main_bp.route("/api/password_reset/request", methods=["POST"])
def password_reset_request():
    """Always answers the same, so it cannot be used to find out which addresses are registered."""
    email = str((request.get_json(silent=True) or {}).get("email") or "").strip()
    if EMAIL_RE.match(email):
        created = db().create_password_reset(email)
        if created:
            username, token = created
            link = f"{config.PUBLIC_SCHEME}://{current_app.config['SERVER_NAME']}/reset_password/{token}"
            send_mail(email, "MCConnect: Passwort zurücksetzen",
                      f"<p>Hallo {username},</p><p>du kannst dein Passwort hier zurücksetzen: "
                      f'<a href="{link}">{link}</a></p><p>Der Link ist 1 Stunde gültig. '
                      f"Wenn du das nicht angefordert hast, ignoriere diese E-Mail.</p>",
                      log_hint=f"password reset link for {username}: {link}")
    return ("", 200)


@main_bp.route("/reset_password/<token>")
def reset_password_page(token):
    username = db().get_password_reset_username(token)
    if username is None:
        return render_template("message.html", ok=False, title="Link ungültig",
                               text="Der Link ist ungültig oder abgelaufen. Fordere einen neuen an."), 400
    return render_template("reset_password.html", username=username, token=token)


@main_bp.route("/api/password_reset/confirm", methods=["POST"])
def password_reset_confirm():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        return {"error": f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein."}, 400
    if db().reset_password(str(data.get("token") or ""), password) is None:
        return {"error": "Der Link ist ungültig oder abgelaufen. Fordere einen neuen an."}, 400
    session.clear()
    return ("", 200)


@main_bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return ("", 200)


@main_bp.route("/verify_email/<username>/<token>")
def verify_email(username, token):
    ok = db().verify_signupcode(username, token)
    return render_template("message.html", ok=ok,
                           title="E-Mail bestätigt" if ok else "Link ungültig",
                           text="Du kannst dich jetzt einloggen." if ok else
                           "Der Link ist ungültig oder abgelaufen. Registriere dich bitte erneut."), 200 if ok else 400


def _validate_server_fields(data, current=None):
    """
    Return (fields, error). When creating (current is None) all fields are required;
    when updating only the fields present in data are validated, merged with the
    current values for the checks that depend on each other.
    """
    fields, specs = {}, {
        "server_name": (1, 64), "mc_server_domain": (0, 253),
        "server_description_short": (1, 200), "server_description_long": (1, 5000), "discord_url": (0, 300),
    }
    for key, (min_len, max_len) in specs.items():
        if key not in data and current is not None:
            continue
        value = str(data.get(key) or "").strip()
        if not min_len <= len(value) <= max_len:
            return None, f"Feld '{key}' muss {min_len}-{max_len} Zeichen lang sein."
        # only the long description may have line breaks (the name ends up in e-mail subjects, for example)
        if any(ord(c) < 0x20 and not (key == "server_description_long" and c in "\r\n\t") for c in value):
            return None, f"Feld '{key}' enthält ungültige Zeichen."
        optional = key in ("mc_server_domain", "discord_url")
        fields[key] = (value or None) if optional else value
    for flag in ("whitelist", "auto_mod_ops", "alerts_enabled", "listed"):
        if flag in data or current is None:
            fields[flag] = data.get(flag) in (True, "true", "on", "1", 1)

    merged = dict(current or {}, **fields)
    domain = merged.get("mc_server_domain")
    if not merged.get("whitelist") and not domain:
        return None, "Öffentliche Server brauchen eine Adresse zum Verbinden."
    if domain and " " in domain:
        return None, "Die Server-Adresse darf keine Leerzeichen enthalten."
    discord = fields.get("discord_url")
    if discord and not re.match(r"^https://(discord\.gg|discord\.com|www\.discord\.com)/\S+$", discord):
        return None, "Der Discord-Link muss mit https://discord.gg/ oder https://discord.com/ beginnen."
    return fields, None


@main_bp.route("/api/servers", methods=["POST"])
@admin_required
def create_server_api():
    data = request.get_json(silent=True) or {}
    subdomain = str(data.get("subdomain") or "").strip().lower()
    if not SUBDOMAIN_RE.match(subdomain):
        return {"error": "Die Subdomain muss 3-32 Zeichen lang sein (a-z, 0-9, -)."}, 400
    if subdomain in RESERVED_SUBDOMAINS:
        return {"error": "Diese Subdomain ist reserviert."}, 400
    fields, error = _validate_server_fields(data)
    if error:
        return {"error": error}, 400
    try:
        server_id = db().add_server(session["admin_id"], subdomain, fields["mc_server_domain"], fields["server_name"],
                                    server_description_short=fields["server_description_short"],
                                    server_description_long=fields["server_description_long"],
                                    discord_url=fields["discord_url"], whitelist=fields["whitelist"])
    except psycopg2.errors.UniqueViolation:
        return {"error": "Diese Subdomain ist bereits vergeben."}, 409
    return {"id": server_id, "subdomain": subdomain}, 201


@main_bp.route("/api/servers/status")
@admin_required
def servers_status_api():
    """Polled by the manage page to show when a plugin connects."""
    return {"servers": [{"id": s["id"], "plugin_online": bool(s["plugin_online"]), "player_count": s["player_count"]}
                        for s in db().get_servers_by_owner(session["admin_id"])]}


@main_bp.route("/api/servers/<int:server_id>/update", methods=["POST"])
@admin_required
def update_server_api(server_id):
    current = {s["id"]: s for s in db().get_servers_by_owner(session["admin_id"])}.get(server_id)
    if current is None:
        abort(404)
    fields, error = _validate_server_fields(request.get_json(silent=True) or {}, current)
    if error:
        return {"error": error}, 400
    if not db().update_server(server_id, session["admin_id"], **fields):
        abort(404)
    return ("", 200)


@main_bp.route("/api/servers/<int:server_id>/regenerate_key", methods=["POST"])
@admin_required
def regenerate_key_api(server_id):
    key = db().regenerate_server_key(server_id, session["admin_id"])
    if key is None:
        abort(404)
    return {"server_key": key}


@main_bp.route("/api/servers/<int:server_id>/delete", methods=["POST"])
@admin_required
def delete_server_api(server_id):
    servers = {s["id"]: s for s in db().get_servers_by_owner(session["admin_id"])}
    if server_id not in servers:
        abort(404)
    if (request.get_json(silent=True) or {}).get("confirm") != servers[server_id]["subdomain"]:
        return {"error": "Zur Bestätigung die Subdomain eingeben."}, 400
    delete_images(db().delete_server(server_id, session["admin_id"]) or [], config.UPLOAD_DIR)
    return ("", 200)


@main_bp.route("/api/servers/<int:server_id>/moderation")
@admin_required
def admin_moderation_api(server_id):
    owned_server_or_404(server_id)
    return {"moderators": db().list_moderators(server_id), "bans": bans_json(server_id)}


@main_bp.route("/api/servers/<int:server_id>/moderators", methods=["POST"])
@admin_required
def admin_set_moderator_api(server_id):
    owned_server_or_404(server_id)
    data = request.get_json(silent=True) or {}
    name = db().set_moderator(server_id, str(data.get("name") or "").strip(), bool(data.get("moderator")))
    if name is None:
        return {"error": "Diesen Spieler gibt es auf dem Server nicht."}, 404
    db().add_mod_log(server_id, f"Admin {session['admin_username']}",
                     "mod_add" if data.get("moderator") else "mod_remove", name)
    return {"name": name}


@main_bp.route("/api/servers/<int:server_id>/ban", methods=["POST"])
@admin_required
def admin_ban_api(server_id):
    owned_server_or_404(server_id)
    return do_ban(server_id, request.get_json(silent=True) or {}, banned_by=f"Admin {session['admin_username']}")


@main_bp.route("/api/servers/<int:server_id>/unban", methods=["POST"])
@admin_required
def admin_unban_api(server_id):
    owned_server_or_404(server_id)
    return do_unban(server_id, request.get_json(silent=True) or {}, unbanned_by=f"Admin {session['admin_username']}")


def owned_server_or_404(server_id):
    server = {s["id"]: s for s in db().get_servers_by_owner(session["admin_id"])}.get(server_id)
    if server is None:
        abort(404)
    return server


@main_bp.route("/api/servers/<int:server_id>/images", methods=["POST"])
@admin_required(allow_upload=True)
def upload_server_image_api(server_id):
    kind = request.form.get("kind")
    file = request.files.get("image")
    if kind not in ("banner", "gallery") or file is None:
        return {"error": "Bild und Art (banner/gallery) angeben."}, 400
    if server_id not in {s["id"] for s in db().get_servers_by_owner(session["admin_id"])}:
        abort(404)
    try:
        filename = save_image(file.stream, kind, config.UPLOAD_DIR)
    except InvalidImage as e:
        return {"error": str(e)}, 400
    result = db().add_server_image(server_id, session["admin_id"], kind, filename)
    if result is None:
        delete_images([filename], config.UPLOAD_DIR)
        return {"error": f"Die Galerie ist voll (maximal {MAX_GALLERY_IMAGES} Bilder)."}, 400
    image_id, replaced = result
    delete_images(replaced, config.UPLOAD_DIR)
    return {"id": image_id, "url": image_url(filename)}, 201


@main_bp.route("/api/servers/<int:server_id>/images/<int:image_id>/delete", methods=["POST"])
@admin_required
def delete_server_image_api(server_id, image_id):
    filename = db().delete_server_image(image_id, session["admin_id"])
    if filename is None:
        abort(404)
    delete_images([filename], config.UPLOAD_DIR)
    return ("", 200)


def image_url(filename):
    """Absolute URL: images are served from the main domain, also on server subdomains."""
    return url_for("main.uploaded_image", filename=filename, _external=True)


@main_bp.route("/uploads/<filename>")
def uploaded_image(filename):
    if not FILENAME_RE.match(filename):
        abort(404)
    # names are random and never reused, so the files can be cached forever
    return send_from_directory(config.UPLOAD_DIR, filename, max_age=31536000)


################################ APP FACTORY #################################

CSP = "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"


def create_app(db_manager=None, config_overrides=None):
    app = Flask(__name__, subdomain_matching=True)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PREFERRED_URL_SCHEME=config.PUBLIC_SCHEME,
        FEATURE_PREFIXES=True,       # prefix pages (/add_pref, /join_pref)
        FEATURE_ADMIN_PANEL=True,    # moderation page for moderators (/users)
        FEATURE_RANKINGS=True,       # rankings and player comparison (/rangliste, /vergleich)
        PROXY_FIX=False,             # set FLASK_PROXY_FIX=true behind traefik/nginx
    )
    app.config.from_pyfile(os.path.join(app.root_path, "config.py"), silent=True)
    app.config.from_pyfile(os.path.join(app.root_path, "instance", "config.py"), silent=True)
    app.config.from_prefixed_env()  # FLASK_SECRET_KEY, FLASK_SERVER_NAME, FLASK_SESSION_COOKIE_SECURE, ...
    app.config.update(config_overrides or {})
    if not app.config.get("SERVER_NAME"):
        app.config["SERVER_NAME"] = config.BASE_DOMAIN
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("No SECRET_KEY configured (web/instance/config.py or FLASK_SECRET_KEY)")
    if app.config["PROXY_FIX"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.extensions["mcconnect_db"] = db_manager or DatabaseManager()
    app.extensions["mcconnect_live"] = LiveCache()
    app.extensions["mcconnect_limits"] = {
        "invite_code": RateLimiter(10, 600),       # per client address, against guessing codes
        "application": RateLimiter(5, 3600),       # per client address, the open applications are capped
        "admin_login": RateLimiter(20, 600),       # per client address
        "admin_login_name": RateLimiter(10, 600),  # per account, against guessing passwords from many addresses
        "login_pin": RateLimiter(5, 900),          # new pins per player
        "login_pin_client": RateLimiter(20, 900),  # new pins per client address
    }
    if config.SMTP_HOST:
        from database.SMTPMailer import SMTPMailer
        app.extensions["mcconnect_mailer"] = SMTPMailer(config.SMTP_HOST, config.SMTP_PORT,
                                                        config.SMTP_USER, config.SMTP_PASSWORD)

    @app.errorhandler(413)
    def too_large(error):
        return {"error": f"Die Datei ist zu groß (maximal {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."}, 413

    @app.after_request
    def add_headers(response):
        # Static files are served from the main domain; server subdomains load the
        # fonts cross-origin, which browsers only allow with a CORS header.
        if request.path.startswith("/static/fonts/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        # no framing by other sites (clickjacking of the moderation and admin pages)
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Partial CSP that works with the inline scripts: no framing, no plugins, no <base> or form
        # targets elsewhere. Scripts and styles follow with nonces during the menu rework.
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    app.register_blueprint(main_bp)
    app.register_blueprint(server_bp)
    logger.info(f"Application started for {app.config['SERVER_NAME']}")
    return app


if __name__ == "__main__":
    create_app().run(debug=True, host="0.0.0.0", threaded=True)
