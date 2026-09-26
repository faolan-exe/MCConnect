"""MCConnect web server.

Main domain (SERVER_NAME, e.g. mc.tobisit.de): landing page, server admin signup/login.
Server pages (<subdomain>.SERVER_NAME): player list, player stats, player login.

Run for development:  python web/main.py
Run in production:    gunicorn 'web.main:create_app()'
"""
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import urlparse

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psycopg2.errors
from colorlogx import get_logger
from flask import (Blueprint, Flask, Response, abort, current_app, g, redirect, render_template,
                   request, session, stream_with_context)

from database import config
from database.databaseManagerV2 import DatabaseManager
from database.stats import format_time

logger = get_logger("webServer")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
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
    server = g.server
    # loginVar is only rendered while logged out (name == "").
    return dict(loginVar="<a href=\"/login\" id=loginLink>Login</a>",
                perm=permission_level if permission_level is not None else 99,
                uuid_profile=session.get("uuid") if player_id else None,
                name=name,
                server_description_short=server["server_description_short"],
                server_description_long=server["server_description_long"],
                discord_url=server["discord_url"],
                license_type=server["license_type"],
                mc_server_domain=server["mc_server_domain"],
                server_name=server["server_name"])


@server_bp.route("/")
def subdomain_index_route():
    return render_template("index-subpage.html")


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
        results = [[p["name"], p["uuid"]] for p in players]
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
        startdate, enddate = start.strftime("%d.%m.%Y %H:%M"), end.strftime("%d.%m.%Y %H:%M")

    return render_template(
        "spieler-info.html", uuid=info["mojang_uuid"], user_name=info["name"], status=info["online"],
        banned=bool(banned), startdate=startdate, enddate=enddate,
        armor_stats=json.dumps(db().get_all_armor_stats(player_id)),
        tool_stats=json.dumps(db().get_all_tools_stats(player_id)),
        item_stats=json.dumps(db().get_all_items_stats(player_id)),
        block_stats=json.dumps(db().get_all_blocks_stats(player_id)),
        mob_stats=json.dumps(db().get_all_mobs_stats(player_id)),
        custom_stats=json.dumps(db().get_all_custom_stats(player_id)))


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
        return [
            str(info["mojang_uuid"]),
            "online" if info["online"] else "offline",
            _custom_stat(database, player_id, "minecraft:deaths"),
            fmt(info["first_seen"]),
            fmt(info["last_seen"]),
            format_time(_custom_stat(database, player_id, "minecraft:time_since_death") / 20),
            format_time(_custom_stat(database, player_id, "minecraft:play_time", "minecraft:play_one_minute") / 20),
        ]
    return sse_response(player_info)


################################ MAIN DOMAIN #################################

@main_bp.route("/")
def main_index_route():
    return render_template("index-main.html")


@main_bp.route("/login")
def server_admin_login():
    return render_template("serverAdminLogin.html")


@main_bp.route("/create")
def create_new_server():
    if not session.get("admin_id"):
        return redirect("/login")
    return render_template("serverAdminCreate.html")


@main_bp.route("/manage")
def manage_server():
    if not session.get("admin_id"):
        return redirect("/login")
    return render_template("serverAdminManage.html")


@main_bp.route("/healthz")
def healthz():
    db()._fetchvalue("SELECT 1")
    return {"status": "ok"}


@main_bp.route("/api/player_count")
def stream_total_player_count():
    database = db()
    return sse_response(database.get_online_player_count_total)


@main_bp.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not USERNAME_RE.match(username):
        return {"error": "invalid username"}, 400
    if not EMAIL_RE.match(email):
        return {"error": "invalid email"}, 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return {"error": "password too short"}, 400

    try:
        admin_id = db().add_server_admin(username, password, email)
    except psycopg2.errors.UniqueViolation:
        return {"error": "username or email already taken"}, 409
    token = db().create_email_verification(admin_id)
    link = f"{config.PUBLIC_SCHEME}://{current_app.config['SERVER_NAME']}/verify_email/{username}/{token}"
    mailer = current_app.extensions.get("mcconnect_mailer")
    if mailer:
        mailer.send_email(email, "MCConnect: E-Mail bestätigen",
                          f'<p>Hallo {username},</p><p>bitte bestätige deine E-Mail-Adresse: '
                          f'<a href="{link}">{link}</a></p>')
    else:
        logger.warning(f"No SMTP configured, verification link for {username}: {link}")
    return ("", 200)


@main_bp.route("/api/login", methods=["POST"])
def server_login_api():
    data = request.get_json(silent=True) or {}
    username = data.get("username") or ""
    if db().verify_admin_login(username, data.get("password") or ""):
        session.clear()
        session["admin_id"] = db().get_admin_id_by_username(username)
        session["admin_username"] = username
        return ("", 200)
    return ("", 400)


@main_bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return ("", 200)


@main_bp.route("/verify_email/<username>/<token>")
def verify_email(username, token):
    if db().verify_signupcode(username, token):
        return ("E-Mail bestätigt, du kannst dich jetzt einloggen.", 200)
    return ("Der Link ist ungültig oder abgelaufen.", 400)


################################ APP FACTORY #################################

def create_app(db_manager=None, config_overrides=None):
    app = Flask(__name__, subdomain_matching=True)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.config.from_pyfile(os.path.join(app.root_path, "config.py"), silent=True)
    app.config.from_pyfile(os.path.join(app.root_path, "instance", "config.py"), silent=True)
    app.config.from_prefixed_env()  # FLASK_SECRET_KEY, FLASK_SERVER_NAME, FLASK_SESSION_COOKIE_SECURE, ...
    app.config.update(config_overrides or {})
    if not app.config.get("SERVER_NAME"):
        app.config["SERVER_NAME"] = config.BASE_DOMAIN
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("No SECRET_KEY configured (web/instance/config.py or FLASK_SECRET_KEY)")

    app.extensions["mcconnect_db"] = db_manager or DatabaseManager()
    if config.SMTP_HOST:
        from database.SMTPMailer import SMTPMailer
        app.extensions["mcconnect_mailer"] = SMTPMailer(config.SMTP_HOST, config.SMTP_PORT,
                                                        config.SMTP_USER, config.SMTP_PASSWORD)

    app.register_blueprint(main_bp)
    app.register_blueprint(server_bp)
    logger.info(f"Application started for {app.config['SERVER_NAME']}")
    return app


if __name__ == "__main__":
    create_app().run(debug=True, host="0.0.0.0", threaded=True)
