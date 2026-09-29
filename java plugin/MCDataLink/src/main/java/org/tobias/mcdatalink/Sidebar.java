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
import java.util.function.BiConsumer;

/**
 * The scoreboard sidebar that players switch on with /seitenleiste (content from MCConnect).
 * Every player with a sidebar gets an own scoreboard; the name tag teams of PrefixDisplay are
 * copied into it, so prefixes above the heads stay visible, and the line under the names (NameBadges) in the
 * variant of its owner (icons or fallbacks). Main thread only.
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
        objective.setDisplayName(cut(color(plugin.glyphs().forViewer(title, uuid)), MAX_TITLE));
        objective.setDisplaySlot(DisplaySlot.SIDEBAR);
        hideScores(objective);
        Set<String> used = new HashSet<>();
        int score = lines.size();
        for (String line : lines) {
            String entry = cut(color(plugin.glyphs().forViewer(line, uuid)), MAX_LINE - 4);
            while (!used.add(entry)) entry += ChatColor.RESET;  // entries must be unique
            objective.getScore(entry).setScore(score--);
        }
        copyTeams(plugin.getServer().getScoreboardManager().getMainScoreboard(), board);
        plugin.nameBadges().apply(board, plugin.glyphs().hasPack(uuid));
        if (player.getScoreboard() != board) player.setScoreboard(board);
    }

    void hide(UUID uuid) {
        Scoreboard board = boards.remove(uuid);
        Player player = plugin.getServer().getPlayer(uuid);
        if (board != null && player != null && player.getScoreboard() == board) {
            player.setScoreboard(plugin.nameBadges().baseBoard(player));
        }
    }

    void quit(Player player) {
        boards.remove(player.getUniqueId());
    }

    /** The sidebar scoreboard of a player, or null. */
    Scoreboard boardOf(UUID uuid) {
        return boards.get(uuid);
    }

    void forEachBoard(BiConsumer<UUID, Scoreboard> action) {
        boards.forEach(action);
    }

    /** After a prefix changed: update the name tag teams in every sidebar scoreboard. */
    void syncTeams() {
        Scoreboard main = plugin.getServer().getScoreboardManager().getMainScoreboard();
        for (Scoreboard board : boards.values()) copyTeams(main, board);
    }

    /** MCConnect's name tag teams of the main scoreboard into another board. */
    static void copyTeams(Scoreboard main, Scoreboard board) {
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

    /** Paper 1.20.3+ can hide the red score numbers on the right; older servers keep them. */
    private static void hideScores(Objective objective) {
        try {
            Class<?> format = Class.forName("io.papermc.paper.scoreboard.numbers.NumberFormat");
            Object blank = format.getMethod("blank").invoke(null);
            Objective.class.getMethod("numberFormat", format).invoke(objective, blank);  // the API interface: the implementation is not public
        } catch (ReflectiveOperationException | LinkageError ignored) {
            // not available on this server
        }
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }

    private static String cut(String text, int max) {
        return text.length() <= max ? text : text.substring(0, max);
    }
}
