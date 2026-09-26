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
import time
import uuid as uuid_mod
from urllib.parse import urlparse

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psycopg2.errors
from colorlogx import get_logger
from flask import (Blueprint, Flask, Response, abort, current_app, g, redirect, render_template,
                   request, send_file, send_from_directory, session, stream_with_context, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from database import config
from database.databaseManagerV2 import MAX_GALLERY_IMAGES, MODERATOR_LEVEL, DatabaseManager
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
SSE_INTERVAL_SECONDS = 2
# SSE streams end after this time; the browser's EventSource reconnects on its own.
# Keeps worker threads from being blocked forever by forgotten tabs.
SSE_MAX_LIFETIME_SECONDS = 300

main_bp = Blueprint("main", __name__)
server_bp = Blueprint("server", __name__, subdomain="<subdomain>")


def db():
    return current_app.extensions["mcconnect_db"]


def sse_response(generate_data, interval=SSE_INTERVAL_SECONDS, lifetime=SSE_MAX_LIFETIME_SECONDS):
    """Stream generate_data() as server sent events every `interval` seconds."""
    def stream():
        deadline = time.monotonic() + lifetime
        while time.monotonic() < deadline:
            yield f"data: {json.dumps(generate_data())}\n\n"
            time.sleep(interval)
    response = Response(stream_with_context(stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # disable nginx buffering
    return response


def safe_next_path(path):
    """Only allow local absolute paths as redirect target."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


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
                server_name=server["server_name"])


@server_bp.route("/")
def subdomain_index_route():
    images = db().get_server_images(g.server["id"])
    return render_template("index-subpage.html",
                           player_total=len(db().get_all_player_ids_from_subdomain(g.subdomain)),
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
        return redirect(f"/login?next={referrer_path}")
    pending_name = session.get("login_name") if session.get("login_server_id") == g.server["id"] else None
    return render_template("login.html", uuid=pending_name or "")


@server_bp.route("/spieler")
def player_overview_route():
    """Player list, or the stats of one player with ?player=<name>."""
    user_name = request.args.get("player")
    if not user_name:
        players = db().get_players_overview_from_subdomain(g.subdomain)
        prefixes = db().get_all_worn_prefixes(g.server["id"])
        results = [[p["name"], p["uuid"], prefixes.get(p["uuid"])] for p in players]
        status = ["online" if p["online"] else "offline" for p in players]
        return render_template("spieler.html", results=results, status=status)

    player_id = db().get_player_id_from_player_name_and_server_id(user_name, g.server["id"])
    if player_id is None:
        abort(404)
    info = db().get_player_info_by_player_id(player_id)

    startdate, enddate = "", ""
    banned = db().get_ban_reason_from_player_id(player_id)
    if banned:
        start, end = db().get_ban_start_and_ban_end_by_player_id(player_id)
        startdate = start.strftime("%d.%m.%Y %H:%M")
        enddate = end.strftime("%d.%m.%Y %H:%M") if end else "dauerhaft"

    return render_template(
        "spieler-info.html", uuid=info["mojang_uuid"], user_name=info["name"], status=info["online"],
        player_prefix=db().get_player_prefix(player_id),
        banned=bool(banned), startdate=startdate, enddate=enddate,
        armor_stats=json.dumps(db().get_all_armor_stats(player_id)),
        tool_stats=json.dumps(db().get_all_tools_stats(player_id)),
        item_stats=json.dumps(db().get_all_items_stats(player_id)),
        block_stats=json.dumps(db().get_all_blocks_stats(player_id)),
        mob_stats=json.dumps(db().get_all_mobs_stats(player_id)),
        custom_stats=json.dumps(db().get_all_custom_stats(player_id)))


def player_required(view):
    """Server pages that need a logged in player; POST API calls must be JSON (CSRF protection)."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        is_api = request.path.startswith("/api/")
        if not logged_in_player_id():
            return ({"error": "Bitte zuerst einloggen."}, 401) if is_api else redirect(f"/login?next={request.path}")
        if is_api and request.method == "POST" and not request.is_json:
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
    return render_template("moderation.html", ban_reasons=db().get_ban_reasons(), bans_api="/api/mod",
                           players=db().get_players_overview_from_subdomain(g.subdomain))


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
    return do_unban(g.server["id"], request.get_json(silent=True) or {})


################################ BANS (shared by moderators and server admins) #################################

def bans_json(server_id):
    fmt = lambda ts: ts.strftime("%d.%m.%Y %H:%M") if ts else None
    return [{"player_id": b["player_id"], "name": b["name"], "uuid": b["uuid"], "source": b["source"],
             "reason": b["reason"], "banned_by": b["banned_by"], "comment": b["comment"],
             "start": fmt(b["start"]), "end": fmt(b["end"])} for b in db().list_active_bans(server_id)]


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
    ban = db().ban_player(server_id, name, banned_by, reason_id=reason_id, days=days, comment=comment)
    if ban is None:
        return {"error": "Diesen Spieler gibt es auf dem Server nicht."}, 404
    end_ms = int(ban["end"].timestamp() * 1000) if ban["end"] else 0
    db().notify_server_event(server_id, "ban", uuid=ban["uuid"], name=ban["name"], reason=ban["reason"], end_ms=end_ms)
    return ("", 200)


def do_unban(server_id, data):
    try:
        player_id = str(uuid_mod.UUID(str(data.get("player_id"))))
    except ValueError:
        return {"error": "Unbekannter Spieler."}, 400
    result = db().unban_player(server_id, player_id)
    if result is None:
        return {"error": "Unbekannter Spieler."}, 404
    db().notify_server_event(server_id, "unban", uuid=result["uuid"], name=result["name"])
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
def stream_player_count():
    subdomain = g.subdomain
    database = db()
    return sse_response(lambda: database.get_online_player_count_from_subdomain(subdomain))


@server_bp.route("/api/status")
def stream_status():
    """Online status of all players, in the same order as the player list."""
    subdomain = g.subdomain
    database = db()
    return sse_response(lambda: ["online" if p["online"] else "offline"
                                 for p in database.get_players_overview_from_subdomain(subdomain)])


def _custom_stat(database, player_id, *names):
    """First existing value of the given custom stats (names differ between game versions)."""
    for name in names:
        value = database.get_value_from_unique_object_from_action_table_with_player_id(name, player_id)
        if value is not None:
            return value
    return 0


@server_bp.route("/api/player_info/<path:player_name>")
def stream_player_info(player_name):
    database = db()
    player_id = database.get_player_id_from_player_name_and_server_id(player_name, g.server["id"])
    if player_id is None:
        abort(404)

    def player_info():
        info = database.get_player_info_by_player_id(player_id)
        fmt = lambda ts: ts.strftime("%d.%m.%Y") if ts else "-"
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
    return sse_response(player_info)




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
    return render_template("index-main.html")


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
        server["images"] = db().get_server_images(server["id"])
        server["players"] = db().get_players_overview_from_subdomain(server["subdomain"])
    return render_template("serverAdminManage.html",
                           servers=servers, max_gallery_images=MAX_GALLERY_IMAGES,
                           ban_reasons=db().get_ban_reasons(),
                           base_domain=current_app.config["SERVER_NAME"],
                           plugin_host=config.PLUGIN_PUBLIC_HOST, plugin_port=config.PLUGIN_PUBLIC_PORT,
                           plugin_available=plugin_jar_path() is not None)


@main_bp.route("/healthz")
def healthz():
    db()._fetchvalue("SELECT 1")
    return {"status": "ok"}


@main_bp.route("/api/player_count")
def stream_total_player_count():
    database = db()
    return sse_response(database.get_online_player_count_total)


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
    admin = db().authenticate_admin(str(data.get("username") or "").strip(), str(data.get("password") or ""))
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
        optional = key in ("mc_server_domain", "discord_url")
        fields[key] = (value or None) if optional else value
    for flag in ("whitelist", "auto_mod_ops"):
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
    return do_unban(server_id, request.get_json(silent=True) or {})


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

def create_app(db_manager=None, config_overrides=None):
    app = Flask(__name__, subdomain_matching=True)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PREFERRED_URL_SCHEME=config.PUBLIC_SCHEME,
        FEATURE_PREFIXES=True,       # prefix pages (/add_pref, /join_pref)
        FEATURE_ADMIN_PANEL=True,    # moderation page for moderators (/users)
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
    if config.SMTP_HOST:
        from database.SMTPMailer import SMTPMailer
        app.extensions["mcconnect_mailer"] = SMTPMailer(config.SMTP_HOST, config.SMTP_PORT,
                                                        config.SMTP_USER, config.SMTP_PASSWORD)

    @app.errorhandler(413)
    def too_large(error):
        return {"error": f"Die Datei ist zu groß (maximal {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."}, 413

    @app.after_request
    def allow_font_embedding(response):
        # Static files are served from the main domain; server subdomains load the
        # fonts cross-origin, which browsers only allow with a CORS header.
        if request.path.startswith("/static/fonts/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    app.register_blueprint(main_bp)
    app.register_blueprint(server_bp)
    logger.info(f"Application started for {app.config['SERVER_NAME']}")
    return app


if __name__ == "__main__":
    create_app().run(debug=True, host="0.0.0.0", threaded=True)
