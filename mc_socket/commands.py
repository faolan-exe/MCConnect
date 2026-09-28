"""In-game commands (/stats, /top, /wettbewerb, /duell, /report, /seitenleiste, /vote, /events) and the sidebar.

The plugin forwards the command (!CMD) and shows the answer lines (!tell). Lines use "&" color
codes, which the plugin turns into chat colors; dynamic text is passed through clean().
"""
import re
from datetime import timedelta

from database import achievements, config, metrics, motivation, rewards

# Names for /top and /duell (what players type), in the order of the tab completion.
METRIC_NAMES = {
    "spielzeit": "play_time", "abgebaut": "blocks_mined", "mobs": "mob_kills", "tode": "deaths",
    "strecke": "distance", "elytra": "distance_elytra", "sprünge": "jumps", "diamanten": "diamonds",
    "debris": "ancient_debris", "smaragde": "emeralds", "gold": "gold", "eisen": "iron",
    "platziert": "blocks_placed", "schaden": "damage_dealt", "erlitten": "damage_taken", "pvp": "player_kills",
    "fische": "fish_caught", "handel": "villager_trades", "zucht": "animals_bred",
}
ALIASES = {"spruenge": "jumps", "springen": "jumps", "bloecke": "blocks_mined", "blöcke": "blocks_mined",
           "netherite": "ancient_debris", "angeln": "fish_caught", "kills": "mob_kills"}
SIDEBAR_MODES = {"aus": "off", "wettbewerb": "competition", "spielzeit": "playtime"}
TOP_COUNT = 5
DUEL_DEFAULT_DAYS = 3
REPORT_MAX_LENGTH = 300
SIDEBAR_LINES = 12
SIDEBAR_WIDTH = 38  # the game cuts scoreboard lines at 40 characters


# Clickable parts of a chat line: ⟦label⇒/command⟧ (plugin 3.10 turns it into a button that runs the command
# as the clicking player; older plugins get the command as text, see without_buttons). clean() removes the
# markers from free text, so players cannot inject buttons.
BUTTON_OPEN, BUTTON_ARROW, BUTTON_CLOSE = "\u27e6", "\u21d2", "\u27e7"
BUTTON_RE = re.compile(f"{BUTTON_OPEN}([^{BUTTON_ARROW}{BUTTON_CLOSE}]*){BUTTON_ARROW}([^{BUTTON_CLOSE}]*){BUTTON_CLOSE}")


def button(label, command):
    return f"{BUTTON_OPEN}{label}{BUTTON_ARROW}{command}{BUTTON_CLOSE}"


def without_buttons(text):
    """
    For plugins without clickable chat: "[Annehmen]" buttons become their command (in the color of the label),
    other labels stay and get the command in brackets unless the label is part of it ("/top spielzeit").
    """
    def plain(match):
        label, command = match.group(1), match.group(2)
        visible = re.sub(r"&[0-9a-fk-or]", "", label).strip()
        if visible.startswith("["):
            colors = "".join(re.findall(r"&[0-9a-fk-or]", label)[:2])
            return f"{colors}{command}"
        if visible.lower() in command.lower():
            return label
        return f"{label} &7({command})"
    return BUTTON_RE.sub(plain, text)


def clean(text):
    """Free text for the chat: no color codes, no protocol separators, no button markers."""
    text = str(text or "").replace("&", "+").replace("§", "").replace("|", "/").replace("~", "-").replace("\n", " ")
    return text.replace(BUTTON_OPEN, "").replace(BUTTON_ARROW, "").replace(BUTTON_CLOSE, "")


def metric_by_name(name):
    name = (name or "").strip().lower()
    key = METRIC_NAMES.get(name) or ALIASES.get(name) or (name if name in metrics.METRICS_BY_KEY else None)
    return metrics.METRICS_BY_KEY.get(key) if key else None


def metric_name(metric):
    """The command name of a metric, e.g. "abgebaut"."""
    return next((name for name, key in METRIC_NAMES.items() if key == metric.key), metric.key)


def page_url(db, server_id, path):
    return f"{config.PUBLIC_SCHEME}://{db.get_subdomain_from_server_id(server_id)}.{config.BASE_DOMAIN}{path}"


class CommandContext:
    """One command of a player. tell(uuid, text) and broadcast(color, text) send to the game."""

    def __init__(self, db, server_id, uuid, location, tell, broadcast, update_sidebar=None, ban=None, mute=None,
                 send=None):
        self.db = db
        self.send = send or (lambda message: None)  # a raw protocol message to the plugin (e.g. !joinstyle)
        self.ban = ban or (lambda result: None)  # kick/ban in the game after ban_player
        self.mute = mute or (lambda uuid, until, reason: None)
        self.server_id = server_id
        self.uuid = uuid
        self.location = location  # (world, x, y, z) or None
        self.tell = tell
        self.broadcast = broadcast
        self.update_sidebar = update_sidebar or (lambda player_id: None)
        self.player_id = db.get_player_id_from_mojang_uuid_and_server_id(uuid, server_id)
        self.name = db.get_player_name_from_player_id(self.player_id) if self.player_id else None


def handle(ctx, command, args):
    """Answer lines for a command (the handlers may also tell other players)."""
    if ctx.player_id is None:
        return ["&cMCConnect kennt dich noch nicht. Warte kurz und versuche es noch einmal."]
    handler = COMMANDS.get(command.lower())
    if handler is None:
        return [f"&cUnbekannter Befehl: {clean(command)}"]
    return handler(ctx, [a for a in args.split(" ") if a])


# ------------------------------------------------------------------ /stats

def cmd_stats(ctx, args):
    db = ctx.db
    if args:
        player_id = db.get_player_id_from_player_name_and_server_id(args[0], ctx.server_id)
        if player_id is None:
            return [f"&c{clean(args[0])} hat noch nie auf dem Server gespielt."]
    else:
        player_id = ctx.player_id
    name = db.get_player_name_from_player_id(player_id)
    if db.is_stats_hidden(player_id) and str(player_id) != str(ctx.player_id):
        return [f"&7{name} zeigt die Statistiken nicht öffentlich."]
    values = db.get_player_metrics(player_id)
    m = metrics.METRICS_BY_KEY
    fmt = lambda key: metrics.format_value(m[key], values[key])
    tiers = sum(achievements.tier_of(a, values[a.metric]) + 1 for a in achievements.ACHIEVEMENTS)
    streak = db.get_player_streak(player_id)
    lines = [f"&6--- Statistik von {name} ---",
             f"&7Spielzeit: &f{fmt('play_time')}  &7Strecke: &f{fmt('distance')}",
             f"&7Abgebaut: &f{fmt('blocks_mined')}  &7Platziert: &f{fmt('blocks_placed')}",
             f"&7Mobs: &f{fmt('mob_kills')}  &7Tode: &f{fmt('deaths')}  &7Diamanterz: &f{fmt('diamonds')}",
             f"&7Erfolge: &f{tiers} von {len(achievements.ACHIEVEMENTS) * len(achievements.TIERS)} Stufen"
             + (f"  &7Serie: &f{streak['current']} {'Tag' if streak['current'] == 1 else 'Tage'} (Rekord {streak['best']})"
                if streak["best"] else "")]
    places = []
    players = db.get_server_metrics(ctx.server_id)
    for metric in (x for x in metrics.METRICS if not x.lower_is_better):
        rank = _rank(players, metric, str(player_id))
        if rank and rank <= 3:
            places.append(f"#{rank} {metric.label}")
    if places:
        lines.append("&7Top-Plätze: &e" + ", ".join(places[:4]))
    lines.append(f"&7Mehr: &b{page_url(db, ctx.server_id, '/spieler?player=' + name)}")
    return lines


def _ranking(players, metric):
    rows = sorted((p for p in players if p["values"][metric.key] > 0),
                  key=lambda p: (p["values"][metric.key] if metric.lower_is_better else -p["values"][metric.key],
                                 p["name"].lower()))
    ranked, previous = [], None
    for index, p in enumerate(rows):
        rank = ranked[-1][0] if previous == p["values"][metric.key] else index + 1
        ranked.append((rank, p))
        previous = p["values"][metric.key]
    return ranked


def _rank(players, metric, player_id):
    return next((rank for rank, p in _ranking(players, metric) if p["player_id"] == player_id), None)


# ------------------------------------------------------------------ /top

def cmd_top(ctx, args):
    metric_buttons = "&7Kennzahlen: " + " ".join(button(f"&f{name}", f"/top {name}") for name in METRIC_NAMES)
    if not args:
        return ["&7Benutzung: &f/top <kennzahl> [7|30]", metric_buttons]
    metric = metric_by_name(args[0])
    if metric is None:
        return [f"&cUnbekannte Kennzahl: {clean(args[0])}", metric_buttons]
    days = int(args[1]) if len(args) > 1 and args[1] in ("7", "30") else None
    players = ctx.db.get_server_metrics(ctx.server_id, days)
    ranking = _ranking(players, metric)
    period = f" ({days} Tage)" if days else ""
    lines = [f"&6--- Top {TOP_COUNT} · {metric.label}{period} ---"]
    if not ranking:
        lines.append("&7Noch keine Werte.")
    for rank, p in ranking[:TOP_COUNT]:
        color = "&e" if rank == 1 else "&f"
        lines.append(f"{color}{rank}. {p['name']} &7– {metrics.format_value(metric, p['values'][metric.key])}")
    mine = next(((rank, p) for rank, p in ranking if p["player_id"] == str(ctx.player_id)), None)
    if mine and all(p["player_id"] != mine[1]["player_id"] for _, p in ranking[:TOP_COUNT]):
        lines.append(f"&7Du: Platz {mine[0]} von {len(ranking)} – {metrics.format_value(metric, mine[1]['values'][metric.key])}")
    return lines


# ------------------------------------------------------------------ /wettbewerb

def cmd_competition(ctx, args):
    db = ctx.db
    today = db.get_today()
    competitions = db.list_competitions(ctx.server_id)
    running = [c for c in competitions if c["starts_on"] <= today <= c["ends_on"]]
    lines = []
    for c in running:
        metric = metrics.METRICS_BY_KEY.get(c["metric"])
        if metric is None:
            continue
        days_left = (c["ends_on"] - today).days + 1
        lines.append(f"&6--- {clean(c['title'])} ---")
        lines.append(f"&7{metric.label}, noch {days_left} {'Tag' if days_left == 1 else 'Tage'} (bis {c['ends_on'].strftime('%d.%m.')})")
        standings = db.get_competition_standings(c)
        for place, row in enumerate(standings[:3], 1):
            lines.append(f"{'&e' if place == 1 else '&f'}{place}. {row['name']} &7– {metrics.format_value(metric, row['value'])}")
        mine = next((i for i, row in enumerate(standings, 1) if row["player_id"] == str(ctx.player_id)), None)
        if mine and mine > 3:
            lines.append(f"&7Du: Platz {mine} – {metrics.format_value(metric, standings[mine - 1]['value'])}")
        elif not mine:
            lines.append("&7Du hast noch keine Punkte – leg los!")
    upcoming = sorted((c for c in competitions if c["starts_on"] > today), key=lambda c: c["starts_on"])
    if not running:
        lines.append("&7Gerade läuft kein Wettbewerb.")
        if upcoming:
            c = upcoming[0]
            lines.append(f"&7Nächster: &f{clean(c['title'])} &7ab {c['starts_on'].strftime('%d.%m.')}")
    for goal in db.list_goals(ctx.server_id):
        metric = metrics.METRICS_BY_KEY.get(goal["metric"])
        if metric is None or goal["reached_at"] or goal["starts_on"] > today or (goal["ends_on"] and goal["ends_on"] < today):
            continue
        total, _ = db.get_goal_progress(goal, today)
        percent = min(100, total * 100 // goal["target"])
        lines.append(f"&aZiel »{clean(goal['title'])}«: &f{percent} % &7({motivation.format_goal_value(metric, total)} von "
                     f"{motivation.format_goal_value(metric, goal['target'])})")
    lines.append(f"&7Alles auf &b{page_url(db, ctx.server_id, '/wettbewerbe')}")
    return lines


# ------------------------------------------------------------------ /duell

def duel_text(db, duel):
    metric = metrics.METRICS_BY_KEY[duel["metric"]]
    gain_c, gain_o = db.duel_gains(duel)
    return (f"{duel['challenger']} {metrics.format_value(metric, gain_c)} : "
            f"{metrics.format_value(metric, gain_o)} {duel['opponent']} ({metric.label})")


def cmd_duel(ctx, args):
    db = ctx.db
    if not args:
        duels = db.get_player_duels(ctx.player_id)
        if not duels:
            return ["&7Du hast keine offenen Duelle.",
                    "&7Herausfordern: &f/duell <spieler> <kennzahl> [tage 1-7]"]
        lines = ["&6--- Deine Duelle ---"]
        for d in duels:
            metric = metrics.METRICS_BY_KEY[d["metric"]]
            if d["status"] == "running":
                lines.append(f"&f{duel_text(db, d)} &7– endet {d['ends_at'].strftime('%d.%m. %H:%M')}")
            elif d["opponent_id"] == str(ctx.player_id):
                lines.append(f"&e{d['challenger']} fordert dich heraus: {metric.label}, {d['days']} Tage "
                             f"{answer_buttons(d['challenger'])}")
            else:
                lines.append(f"&7Wartet auf {d['opponent']}: {metric.label}, {d['days']} Tage "
                             + button("&c[Zurückziehen]", f"/duell zurückziehen {d['opponent']}"))
        return lines

    if args[0].lower() in ("zurückziehen", "zurueckziehen", "abbrechen"):
        mine = [d for d in db.get_player_duels(ctx.player_id, ("pending",)) if d["challenger_id"] == str(ctx.player_id)]
        if len(args) > 1:
            mine = [d for d in mine if d["opponent"].lower() == args[1].lower()]
        if not mine:
            return ["&7Du hast keine offene Herausforderung verschickt."]
        return withdraw(ctx, mine[0]["id"])

    if args[0].lower() in ("annehmen", "ablehnen"):
        accept = args[0].lower() == "annehmen"
        pending = [d for d in db.get_player_duels(ctx.player_id, ("pending",)) if d["opponent_id"] == str(ctx.player_id)]
        if len(args) > 1:
            pending = [d for d in pending if d["challenger"].lower() == args[1].lower()]
        if not pending:
            return ["&7Du hast keine offene Herausforderung."]
        return respond(ctx, pending[0]["id"], accept)

    if len(args) < 2:
        return ["&7Benutzung: &f/duell <spieler> <kennzahl> [tage 1-7]"]
    opponent_id = db.get_player_id_from_player_name_and_server_id(args[0], ctx.server_id)
    if opponent_id is None:
        return [f"&c{clean(args[0])} hat noch nie auf dem Server gespielt."]
    metric = metric_by_name(args[1])
    if metric is None or metric.lower_is_better:
        return [f"&cUnbekannte Kennzahl: {clean(args[1])}", "&7Kennzahlen: &f" + ", ".join(
            n for n, k in METRIC_NAMES.items() if not metrics.METRICS_BY_KEY[k].lower_is_better)]
    try:
        days = int(args[2]) if len(args) > 2 else DUEL_DEFAULT_DAYS
    except ValueError:
        days = 0
    if not 1 <= days <= 7:
        return ["&cEin Duell dauert 1 bis 7 Tage."]
    return challenge(ctx, opponent_id, metric, days)


def challenge(ctx, opponent_id, metric, days):
    """Create a duel and tell the opponent. Returns the answer lines for the challenger."""
    db = ctx.db
    duel_id, error = db.create_duel(ctx.server_id, ctx.player_id, opponent_id, metric.key, days)
    opponent = db.get_player_name_from_player_id(opponent_id)
    if error == "self":
        return ["&cDu kannst dich nicht selbst herausfordern."]
    if error == "hidden":
        return ["&cDuelle gehen nur, wenn ihr beide eure Statistiken öffentlich zeigt."]
    if error == "open":
        return [f"&cMit {opponent} hast du schon ein offenes Duell."]
    ctx.tell(str(db.get_mojang_uuid_from_player_id(opponent_id)), challenge_text(ctx.name, metric, days))
    return [f"&aHerausforderung an {opponent} geschickt: {metric.label}, {days} {'Tag' if days == 1 else 'Tage'}.",
            "&7Sie gilt 24 Stunden. " + button("&c[Zurückziehen]", f"/duell zurückziehen {opponent}")]


def answer_buttons(challenger):
    return (button("&a&l[Annehmen]", f"/duell annehmen {challenger}") + " "
            + button("&c[Ablehnen]", f"/duell ablehnen {challenger}"))


def challenge_text(challenger, metric, days):
    """The invitation the challenged player gets (also sent when the challenge comes from the website)."""
    return (f"&6{challenger} fordert dich zum Duell heraus: &f{metric.label}, {days} {'Tag' if days == 1 else 'Tage'}. "
            + answer_buttons(challenger))


def withdraw(ctx, duel_id):
    duel = ctx.db.cancel_duel(duel_id, ctx.player_id)
    if duel is None:
        return ["&7Die Herausforderung gibt es nicht mehr."]
    ctx.tell(duel["opponent_uuid"], f"&7{duel['challenger']} hat die Herausforderung zum Duell zurückgezogen.")
    return [f"&7Herausforderung an {duel['opponent']} zurückgezogen."]


def respond(ctx, duel_id, accept):
    db = ctx.db
    duel = db.respond_duel(duel_id, ctx.player_id, accept)
    if duel is None:
        return ["&7Die Herausforderung gibt es nicht mehr."]
    metric = metrics.METRICS_BY_KEY[duel["metric"]]
    if accept:
        ctx.broadcast("gold", f"⚔ Duell: {duel['challenger']} gegen {duel['opponent']} – wer schafft in {duel['days']} "
                              f"{'Tag' if duel['days'] == 1 else 'Tagen'} mehr {metric.label}?")
        return [f"&aDuell angenommen! Es endet am {duel['ends_at'].strftime('%d.%m. um %H:%M')}."]
    ctx.tell(duel["challenger_uuid"], f"&7{duel['opponent']} hat dein Duell abgelehnt.")
    return ["&7Duell abgelehnt."]


def duel_result(duel):
    """(color, chat text) for a finished duel."""
    metric = metrics.METRICS_BY_KEY[duel["metric"]]
    c, o = duel["challenger_gain"], duel["opponent_gain"]
    score = f"{metrics.format_value(metric, c)} : {metrics.format_value(metric, o)}"
    if c == o:
        return "gold", f"⚔ Duell {duel['challenger']} gegen {duel['opponent']} endet unentschieden ({score}, {metric.label})."
    winner, loser = (duel["challenger"], duel["opponent"]) if c > o else (duel["opponent"], duel["challenger"])
    return "gold", f"⚔ {winner} gewinnt das Duell gegen {loser}: {score} ({metric.label})!"


# ------------------------------------------------------------------ /report

def cmd_report(ctx, args):
    if len(args) < 2:
        return ["&7Benutzung: &f/report <spieler oder -> <was ist passiert>",
                "&7Deine Position wird mitgeschickt, damit die Moderatoren nachsehen können."]
    db = ctx.db
    target = None if args[0] == "-" else args[0]
    if target:
        target_id = db.get_player_id_from_player_name_and_server_id(target, ctx.server_id)
        target = db.get_player_name_from_player_id(target_id) if target_id else clean(target)[:16]
    reason = " ".join(args[1:])[:REPORT_MAX_LENGTH]
    report_id = db.create_report(ctx.server_id, ctx.player_id, target, reason, ctx.location)
    if report_id is None:
        return ["&cDu hast in der letzten Stunde schon genug gemeldet. Versuche es später noch einmal."]
    notify_moderators(ctx.db, ctx.server_id, ctx.tell, ctx.name, target, ctx.location)
    return ["&aDanke! Deine Meldung ist bei den Moderatoren angekommen."]


def notify_moderators(db, server_id, tell, reporter, target, location):
    where = f" bei {location[0]} {location[1]} {location[2]} {location[3]}" if location else ""
    text = f"&c[Meldung] &f{reporter}{' meldet ' + target if target else ' hat etwas gemeldet'}{clean(where)} &7– " \
           f"&b{page_url(db, server_id, '/users/spieler#reports')}"
    for uuid in db.get_online_moderator_uuids(server_id):
        tell(uuid, text)


# ------------------------------------------------------------------ /seitenleiste

def cmd_sidebar(ctx, args):
    db = ctx.db
    if args and args[0].lower() in SIDEBAR_MODES:
        mode = SIDEBAR_MODES[args[0].lower()]
    elif args:
        return ["&7Benutzung: &f/seitenleiste [aus|wettbewerb|spielzeit]"]
    else:  # without argument: switch to the next mode
        order = list(SIDEBAR_MODES.values())
        mode = order[(order.index(db.get_sidebar(ctx.player_id)) + 1) % len(order)]
    db.set_sidebar(ctx.player_id, mode)
    ctx.update_sidebar(ctx.player_id)
    label = {v: k for k, v in SIDEBAR_MODES.items()}[mode]
    others = " ".join(button(f"&7[{name}]", f"/seitenleiste {name}") for name, value in SIDEBAR_MODES.items() if value != mode)
    return [f"&aSeitenleiste: {label}. " + others]


def sidebar(db, player_id, mode):
    """(title, lines) of the sidebar of a player, or None to hide it."""
    if mode == "competition":
        today = db.get_today()
        running = sorted((c for c in db.list_competitions(db.get_server_id_from_player_id(player_id))
                          if c["starts_on"] <= today <= c["ends_on"]), key=lambda c: c["ends_on"])
        if not running or running[0]["metric"] not in metrics.METRICS_BY_KEY:
            return "Wettbewerb", ["Gerade läuft keiner.", "/seitenleiste spielzeit"]
        c = running[0]
        metric = metrics.METRICS_BY_KEY[c["metric"]]
        standings = db.get_competition_standings(c)
        lines = [f"&7{metric.label}, bis {c['ends_on'].strftime('%d.%m.')}"]
        for place, row in enumerate(standings[:5], 1):
            lines.append(f"{'&e' if place == 1 else '&f'}{place}. {row['name']} &7{metrics.format_value(metric, row['value'])}")
        mine = next((i for i, row in enumerate(standings, 1) if row["player_id"] == str(player_id)), None)
        if mine and mine > 5:
            lines.append(f"&aDu: {mine}. {metrics.format_value(metric, standings[mine - 1]['value'])}")
        elif not mine:
            lines.append("&7Du: noch keine Punkte")
        return _cut(clean(c["title"])), [_cut(line) for line in lines[:SIDEBAR_LINES]]
    if mode == "playtime":
        today = db.get_today()
        play_time = metrics.METRICS_BY_KEY["play_time"]
        week_start = today - timedelta(days=today.weekday())
        streak = db.get_player_streak(player_id)
        return "Deine Spielzeit", [
            f"&7Heute: &f{metrics.format_value(play_time, db.get_player_gain(player_id, 'play_time', today))}",
            f"&7Diese Woche: &f{metrics.format_value(play_time, db.get_player_gain(player_id, 'play_time', week_start))}",
            f"&7Gesamt: &f{metrics.format_value(play_time, db.get_player_metrics(player_id)['play_time'])}",
            f"&7Serie: &f{streak['current']} {'Tag' if streak['current'] == 1 else 'Tage'}",
        ]
    return None


def _cut(text):
    return text if len(text) <= SIDEBAR_WIDTH else text[:SIDEBAR_WIDTH - 1] + "…"


# ------------------------------------------------------------------ /vote

def cmd_vote(ctx, args):
    db = ctx.db
    polls = db.list_polls(ctx.server_id, open_only=True)
    if not polls:
        return ["&7Gerade gibt es keine Umfrage."]
    if len(args) < 2:
        mine = db.get_player_votes(ctx.player_id)
        lines = []
        for number, poll in enumerate(polls, 1):
            lines.append(f"&6Umfrage {number}: &f{clean(poll['question'])} &7(bis {poll['ends_at'].strftime('%d.%m. %H:%M')})")
            for index, option in enumerate(poll["options"], 1):
                chosen = " &a← deine Stimme" if mine.get(poll["id"]) == index - 1 else ""
                lines.append(f"&7  {index}. " + button(f"&f{clean(option)}", f"/vote #{poll['id']} {index}") + chosen)
        lines.append("&7Klick auf eine Antwort oder: &f/vote <umfrage> <antwort>")
        return lines
    try:
        poll = by_reference(polls, args[0])
        option = int(args[1]) - 1
    except (ValueError, IndexError):
        return ["&cDiese Umfrage gibt es nicht. &7/vote zeigt alle."]
    result = db.vote(ctx.server_id, poll["id"], ctx.player_id, option)
    if result != "ok":
        return ["&cDiese Antwort gibt es nicht." if result == "invalid" else "&cDie Umfrage ist schon vorbei."]
    return [f"&aDanke! Deine Stimme: {clean(poll['options'][option])}"]


def poll_result(poll):
    """(color, chat text) for a poll that ended."""
    if not poll["total"]:
        return "gold", f"★ Umfrage »{clean(poll['question'])}« ist vorbei – niemand hat abgestimmt."
    top = max(poll["votes"])
    winners = [clean(o) for o, v in zip(poll["options"], poll["votes"]) if v == top]
    share = round(top * 100 / poll["total"])
    return "gold", (f"★ Umfrage »{clean(poll['question'])}«: {' und '.join(winners)} "
                    f"({share} %, {poll['total']} {'Stimme' if poll['total'] == 1 else 'Stimmen'})")


# ------------------------------------------------------------------ /events

def cmd_events(ctx, args):
    db = ctx.db
    events = db.list_events(ctx.server_id, limit=5)
    if not events:
        return ["&7Gerade ist kein Event geplant."]
    if args and args[0].lower() in ("anmelden", "abmelden"):
        try:
            event = by_reference(events, args[1]) if len(args) > 1 else events[0]
        except (ValueError, IndexError):
            return ["&cDieses Event gibt es nicht. &7/events zeigt alle."]
        signed = db.toggle_event_signup(ctx.server_id, event["id"], ctx.player_id, args[0].lower() == "anmelden")
        if signed is None:
            return ["&cDieses Event ist schon vorbei."]
        return [f"&aDu bist für »{clean(event['title'])}« angemeldet." if signed
                else f"&7Du bist von »{clean(event['title'])}« abgemeldet."]
    mine = db.get_player_event_ids(ctx.player_id)
    lines = ["&6--- Nächste Events ---"]
    for number, event in enumerate(events, 1):
        place = f" &7@ {clean(event['place'])}" if event["place"] else ""
        action = (button("&c[Abmelden]", f"/events abmelden #{event['id']}") if event["id"] in mine
                  else button("&a[Anmelden]", f"/events anmelden #{event['id']}"))
        lines.append(f"&e{number}. &f{clean(event['title'])} &7– {event['starts_at'].strftime('%d.%m. %H:%M')} Uhr{place} "
                     f"&7({event['signups']} dabei) " + action)
    return lines


def by_reference(items, reference):
    """The item for "#<id>" (from a button, stays right when the list changes) or a 1-based number."""
    if reference.startswith("#"):
        wanted = int(reference[1:])
        item = next((item for item in items if item["id"] == wanted), None)
        if item is None:  # e.g. a button of a poll that has ended since
            raise IndexError(reference)
        return item
    number = int(reference)
    if number < 1:
        raise IndexError(reference)
    return items[number - 1]


def event_announcement(kind, event):
    """(color, text) of an event: "new" (just created), "reminder" (soon) or "start"."""
    place = f" Treffpunkt: {clean(event['place'])}." if event["place"] else ""
    signup = " " + button("&a[Anmelden]", f"/events anmelden #{event['id']}")
    if kind == "new":
        when = event["starts_at"].strftime("%d.%m. um %H:%M Uhr")
        return "gold", f"★ Neues Event: »{clean(event['title'])}« am {when}.{place}{signup}"
    if kind == "reminder":
        return "gold", f"★ In Kürze: »{clean(event['title'])}« um {event['starts_at'].strftime('%H:%M')} Uhr.{place}{signup}"
    return "gold", f"★ Jetzt geht's los: »{clean(event['title'])}«!{place}"


# ------------------------------------------------------------------ moderation: /verwarnen, /stumm, /entstummen

MUTE_MAX_MINUTES = 7 * 24 * 60
WARN_REASON_MAX = 200


def apply_warning(db, server_id, player_id, reason, actor, tell, ban):
    """
    Warn a player: store it, tell the player and ban automatically at the threshold (ban(result of
    ban_player) kicks the player). Returns the text for the moderator.
    """
    result = db.warn_player(server_id, player_id, reason, actor)
    name = db.get_player_name_from_player_id(player_id)
    uuid = str(db.get_mojang_uuid_from_player_id(player_id))
    of = f" ({result['count']}/{result['threshold']})" if result["threshold"] else ""
    db.add_mod_log(server_id, actor, "warn", name, reason)
    tell(uuid, f"&c&lVerwarnung{of}: &f{clean(reason)}")
    if result["ban"]:
        ban(result["ban"])
        db.add_mod_log(server_id, actor, "ban", name, f"automatisch nach {result['count']} Verwarnungen")
        return f"{name} ist verwarnt{of} und für {db.get_server_settings(server_id)['warn_ban_days']} Tage gebannt."
    return f"{name} ist verwarnt{of}."


def mute_text(until):
    return f"bis {until.strftime('%d.%m. %H:%M')} Uhr"


def _moderator_target(ctx, args, usage):
    if not ctx.db.is_moderator(ctx.player_id):
        return None, ["&cDas dürfen nur Moderatoren."]
    if len(args) < 1:
        return None, [f"&7Benutzung: &f{usage}"]
    target = ctx.db.get_player_id_from_player_name_and_server_id(args[0], ctx.server_id)
    if target is None:
        return None, [f"&c{clean(args[0])} hat noch nie auf dem Server gespielt."]
    return target, None


def cmd_warn(ctx, args):
    target, error = _moderator_target(ctx, args, "/verwarnen <spieler> <grund>")
    if error:
        return error
    reason = " ".join(args[1:])[:WARN_REASON_MAX]
    if len(reason) < 3:
        return ["&7Benutzung: &f/verwarnen <spieler> <grund>"]
    text = apply_warning(ctx.db, ctx.server_id, target, reason, ctx.name, ctx.tell, ctx.ban)
    return [f"&a{text}"]


def cmd_mute(ctx, args):
    target, error = _moderator_target(ctx, args, "/stumm <spieler> <minuten> [grund]")
    if error:
        return error
    try:
        minutes = int(args[1]) if len(args) > 1 else 0
    except ValueError:
        minutes = 0
    if not 1 <= minutes <= MUTE_MAX_MINUTES:
        return [f"&cDauer in Minuten angeben (1 bis {MUTE_MAX_MINUTES})."]
    reason = " ".join(args[2:])[:WARN_REASON_MAX] or None
    until = mute_player(ctx.db, ctx.server_id, target, minutes, reason, ctx.name, ctx.mute, ctx.tell)
    return [f"&a{ctx.db.get_player_name_from_player_id(target)} ist stummgeschaltet {mute_text(until)}."]


def cmd_unmute(ctx, args):
    target, error = _moderator_target(ctx, args, "/entstummen <spieler>")
    if error:
        return error
    unmute_player(ctx.db, ctx.server_id, target, ctx.name, ctx.mute, ctx.tell)
    return [f"&a{ctx.db.get_player_name_from_player_id(target)} darf wieder schreiben."]


def mute_player(db, server_id, player_id, minutes, reason, actor, mute, tell):
    """Mute for `minutes` minutes; mute(uuid, until or None, reason) tells the plugin. Returns until."""
    until = db.get_now() + timedelta(minutes=minutes)
    db.set_mute(player_id, until, reason)
    uuid = str(db.get_mojang_uuid_from_player_id(player_id))
    mute(uuid, until, reason)
    tell(uuid, f"&cDu bist stummgeschaltet {mute_text(until)}" + (f": &f{clean(reason)}" if reason else "."))
    db.add_mod_log(server_id, actor, "mute", db.get_player_name_from_player_id(player_id),
                   f"{minutes} Min." + (f" · {reason}" if reason else ""))
    return until


def unmute_player(db, server_id, player_id, actor, mute, tell):
    db.set_mute(player_id, None)
    uuid = str(db.get_mojang_uuid_from_player_id(player_id))
    mute(uuid, None, None)
    tell(uuid, "&aDu darfst wieder im Chat schreiben.")
    db.add_mod_log(server_id, actor, "unmute", db.get_player_name_from_player_id(player_id))


# ------------------------------------------------------------------ /joinmessage

# what players type -> field of the style
JOIN_FIELDS = {"text": "join_text", "leave": "leave_text", "farbe": "color", "symbol": "symbol", "stil": "style",
               "sound": "sound"}


def _options(state, field, kind, labels):
    """Buttons of one setting: unlocked options (the chosen one marked), those of the next level gray."""
    style, first = state["style"], rewards.unlocked_at(state["levels"])
    parts = []
    for value in rewards.UNLOCK_KINDS[kind] + (rewards.MOD_COLORS if kind == "colors" and state["moderator"] else ()):
        label = clean(labels(value))
        argument = str(rewards.SYMBOLS.index(value)) if field == "symbol" else value
        command = next(k for k, v in JOIN_FIELDS.items() if v == field)
        if value in state["available"][kind]:
            color = f"&{rewards.COLOR_CODES[value][0]}" if kind == "colors" else "&f"
            mark = "&a✔" if style[field] == value else ""
            parts.append(button(f"{mark}{color}[{label}]", f"/joinmessage {command} {argument}"))
        elif first.get((kind, value)) == state["level"] + 1:  # a taste of the next level, not everything locked
            parts.append(f"&8[{label}]")
    if field == "sound":
        parts.insert(0, button(f"{'&a✔' if not style['sound'] else ''}&f[Aus]", "/joinmessage sound aus"))
    return " ".join(parts)


def cmd_joinmessage(ctx, args):
    db = ctx.db
    state = rewards.player_state(db, ctx.player_id)
    if not state["enabled"]:
        return ["&7Eigene Join-Nachrichten sind auf diesem Server ausgeschaltet."]
    if args:
        return set_join_style(ctx, state, args[0].lower(), args[1] if len(args) > 1 else "")
    style, available = state["style"], state["available"]
    levels, level = state["levels"], state["level"]
    lines = [f"&6--- Deine Join-Nachricht · Stufe {clean(levels[level]['name'])} ({level + 1}/{len(levels)}) ---"]
    if state["custom"]:
        lines += [f"&7Join: {rewards.render(ctx.name, style, available, 'join')}",
                  f"&7Leave: {rewards.render(ctx.name, style, available, 'leave')}"]
    else:
        lines.append("&7Gerade zeigt das Spiel seine normale Nachricht. Wähl etwas aus, um deine eigene zu nutzen.")
    lines += [
        "&7Text: " + _options(state, "join_text", "join_texts", lambda v: rewards.JOIN_TEXTS[v].replace("{name}", "…")),
        "&7Leave: " + _options(state, "leave_text", "leave_texts", lambda v: rewards.LEAVE_TEXTS[v].replace("{name}", "…")),
        "&7Farbe: " + _options(state, "color", "colors", lambda v: rewards.COLOR_LABELS[v]),
        "&7Symbol: " + _options(state, "symbol", "symbols", lambda v: v or "keins"),
        "&7Stil: " + _options(state, "style", "styles", lambda v: rewards.STYLES[v].split(" (")[0]),
        "&7Sound: " + _options(state, "sound", "sounds", lambda v: rewards.SOUNDS[v][0].split(" (")[0]),
    ]
    if level + 1 < len(levels):
        following = levels[level + 1]
        lines.append(f"&7Nächste Stufe »{clean(following['name'])}«: {clean(rewards.level_hint(following))}")
    if state["custom"]:
        lines.append(button("&7[Normale Nachricht des Spiels]", "/joinmessage aus"))
    lines.append(f"&7Vorschau und Stummschalten: &b{page_url(db, ctx.server_id, '/profil')}")
    return lines


def set_join_style(ctx, state, what, value):
    db = ctx.db
    if what == "aus":
        db.set_join_style(ctx.player_id, None)
        ctx.send(rewards.join_style_message(db, ctx.player_id))
        return ["&7Du hast wieder die normale Join-Nachricht des Spiels."]
    field = JOIN_FIELDS.get(what)
    if field is None:
        return ["&7Benutzung: &f/joinmessage [text|leave|farbe|symbol|stil|sound|aus] [wert]"]
    if field == "symbol":
        try:
            value = rewards.SYMBOLS[int(value)]
        except (ValueError, IndexError):
            return ["&cDieses Symbol gibt es nicht."]
    if field == "sound" and value == "aus":
        value = ""
    error = rewards.check_choice(field, value, state["available"])
    if error:
        return [f"&c{error}"]
    style = dict(state["style"], **{field: value})
    db.set_join_style(ctx.player_id, style)
    ctx.send(rewards.join_style_message(db, ctx.player_id))
    return [f"&aGespeichert: {rewards.render(ctx.name, style, state['available'], 'join')}"]


COMMANDS = {"stats": cmd_stats, "top": cmd_top, "wettbewerb": cmd_competition, "duell": cmd_duel,
            "report": cmd_report, "seitenleiste": cmd_sidebar, "vote": cmd_vote, "events": cmd_events,
            "verwarnen": cmd_warn, "stumm": cmd_mute, "entstummen": cmd_unmute, "joinmessage": cmd_joinmessage}
