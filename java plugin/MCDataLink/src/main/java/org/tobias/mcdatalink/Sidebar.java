package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.scoreboard.DisplaySlot;
import org.bukkit.scoreboard.Objective;
import org.bukkit.scoreboard.Scoreboard;
import org.bukkit.scoreboard.Team;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * The scoreboard sidebar that players switch on with /seitenleiste (content from MCConnect).
 * Every player with a sidebar gets an own scoreboard; the name tag teams of PrefixDisplay are
 * copied into it, so prefixes above the heads stay visible. Main thread only.
 */
final class Sidebar {
    private static final String OBJECTIVE = "mccside";
    private static final String TEAM_PREFIX = "mcc";
    private static final int MAX_TITLE = 32;
    private static final int MAX_LINE = 40;

    private final MCDataLink plugin;
    private final Map<UUID, Scoreboard> boards = new HashMap<>();

    Sidebar(MCDataLink plugin) {
        this.plugin = plugin;
    }

    void show(UUID uuid, String title, List<String> lines) {
        Player player = plugin.getServer().getPlayer(uuid);
        if (player == null) return;
        Scoreboard board = boards.get(uuid);
        if (board == null) {
            board = plugin.getServer().getScoreboardManager().getNewScoreboard();
            boards.put(uuid, board);
        }
        Objective old = board.getObjective(OBJECTIVE);
        if (old != null) old.unregister();
        @SuppressWarnings("deprecation")  // the 1.13 API has no criteria enum yet
        Objective objective = board.registerNewObjective(OBJECTIVE, "dummy");
        objective.setDisplayName(cut(color(title), MAX_TITLE));
        objective.setDisplaySlot(DisplaySlot.SIDEBAR);
        Set<String> used = new HashSet<>();
        int score = lines.size();
        for (String line : lines) {
            String entry = cut(color(line), MAX_LINE - 4);
            while (!used.add(entry)) entry += ChatColor.RESET;  // entries must be unique
            objective.getScore(entry).setScore(score--);
        }
        copyTeams(board);
        if (player.getScoreboard() != board) player.setScoreboard(board);
    }

    void hide(UUID uuid) {
        Scoreboard board = boards.remove(uuid);
        Player player = plugin.getServer().getPlayer(uuid);
        if (board != null && player != null && player.getScoreboard() == board) {
            player.setScoreboard(plugin.getServer().getScoreboardManager().getMainScoreboard());
        }
    }

    void quit(Player player) {
        boards.remove(player.getUniqueId());
    }

    /** After a prefix changed: update the name tag teams in every sidebar scoreboard. */
    void syncTeams() {
        for (Scoreboard board : boards.values()) copyTeams(board);
    }

    private void copyTeams(Scoreboard board) {
        Scoreboard main = plugin.getServer().getScoreboardManager().getMainScoreboard();
        for (Team team : board.getTeams()) {
            if (team.getName().startsWith(TEAM_PREFIX) && main.getTeam(team.getName()) == null) team.unregister();
        }
        for (Team source : main.getTeams()) {
            if (!source.getName().startsWith(TEAM_PREFIX)) continue;
            Team copy = board.getTeam(source.getName());
            if (copy == null) {
                copy = board.registerNewTeam(source.getName());
                copy.setPrefix(source.getPrefix());
            }
            for (String entry : copy.getEntries()) {
                if (!source.hasEntry(entry)) copy.removeEntry(entry);
            }
            for (String entry : source.getEntries()) {
                if (!copy.hasEntry(entry)) copy.addEntry(entry);
            }
        }
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }

    private static String cut(String text, int max) {
        return text.length() <= max ? text : text.substring(0, max);
    }
}
