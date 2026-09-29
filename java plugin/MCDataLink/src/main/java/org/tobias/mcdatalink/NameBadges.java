package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.scoreboard.DisplaySlot;
import org.bukkit.scoreboard.Objective;
import org.bukkit.scoreboard.Score;
import org.bukkit.scoreboard.Scoreboard;

import java.lang.reflect.Method;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The line under the players' names (!badge, chosen on the website: trophies, streak, ...). It is a below-name
 * scoreboard objective whose scores show a fixed text instead of the number, which needs Paper 1.20.3+ (found by
 * reflection; on other servers the feature stays off and nothing else changes).
 *
 * A scoreboard shows the same text to everyone who uses it, so the icons need their own board: players whose client
 * loaded the resource pack use a copy of the main scoreboard with MCConnect's name tag teams and the icon line,
 * all others keep the main scoreboard with the Unicode fallbacks. Sidebar boards get the variant of their owner.
 * Main thread only (except set()).
 */
final class NameBadges {
    private static final String OBJECTIVE = "mccbadge";

    private final MCDataLink plugin;
    private final boolean enabled;
    /** uuid -> line with § color codes and icons */
    private final Map<UUID, String> lines = new ConcurrentHashMap<>();
    private Scoreboard packBoard;

    private Method scoreFormat;
    private Method objectiveFormat;
    private Method fixed;
    private Method blank;
    private Object serializer;
    private Method deserialize;

    NameBadges(MCDataLink plugin, boolean wanted) {
        this.plugin = plugin;
        boolean supported = false;
        if (wanted) {
            try {
                // methods of the API interfaces: the server's implementation classes are not public
                Class<?> numberFormat = Class.forName("io.papermc.paper.scoreboard.numbers.NumberFormat");
                Class<?> componentLike = Class.forName("net.kyori.adventure.text.ComponentLike");
                fixed = numberFormat.getMethod("fixed", componentLike);
                blank = numberFormat.getMethod("blank");
                Class<?> legacy = Class.forName("net.kyori.adventure.text.serializer.legacy.LegacyComponentSerializer");
                serializer = legacy.getMethod("legacySection").invoke(null);
                deserialize = legacy.getMethod("deserialize", String.class);
                scoreFormat = Score.class.getMethod("numberFormat", numberFormat);
                objectiveFormat = Objective.class.getMethod("numberFormat", numberFormat);
                supported = true;
            } catch (ReflectiveOperationException | LinkageError e) {
                plugin.getLogger().info("The line under the player names needs Paper 1.20.3 or newer; it stays off on this server.");
            }
        }
        enabled = supported;
        if (enabled) {  // a leftover of a crash would show old lines until the first update
            Objective old = main().getObjective(OBJECTIVE);
            if (old != null) old.unregister();
        }
    }

    boolean enabled() {
        return enabled;
    }

    /** From the connection thread: !badge uuid|line (empty: nothing). */
    void set(UUID uuid, String line) {
        if (!enabled) return;
        if (line.isEmpty()) lines.remove(uuid);
        else lines.put(uuid, ChatColor.translateAlternateColorCodes('&', line));
        plugin.runOnMainThread(() -> {
            Player player = plugin.getServer().getPlayer(uuid);
            if (player == null) return;
            forEachBoard((board, icons) -> show(board, icons, player));
        });
    }

    /** The scoreboard of a player without a sidebar: the icon board if their client loaded the pack. */
    Scoreboard baseBoard(Player player) {
        return enabled && plugin.glyphs().hasPack(player.getUniqueId()) ? packBoard() : main();
    }

    /** After the player's client loaded (or dropped) the pack: switch to the board with the matching variant. */
    void viewerChanged(Player player) {
        if (!enabled) return;
        Scoreboard sidebarBoard = plugin.sidebar().boardOf(player.getUniqueId());
        if (sidebarBoard != null) {
            apply(sidebarBoard, plugin.glyphs().hasPack(player.getUniqueId()));
            return;
        }
        Scoreboard current = player.getScoreboard();
        if (current != main() && current != packBoard) return;  // another plugin's scoreboard: leave it alone
        Scoreboard wanted = baseBoard(player);
        if (current != wanted) player.setScoreboard(wanted);
    }

    /** Main thread: the objective with every online player's line on a (new) board. */
    void apply(Scoreboard board, boolean icons) {
        if (!enabled) return;
        for (Player player : plugin.getServer().getOnlinePlayers()) show(board, icons, player);
    }

    /** Main thread: a player joined. */
    void joined(Player player) {
        if (!enabled) return;
        forEachBoard((board, icons) -> show(board, icons, player));
        viewerChanged(player);
    }

    /** Main thread: a player left; their score is removed so the main scoreboard does not keep it. */
    void quit(Player player) {
        if (!enabled) return;
        lines.remove(player.getUniqueId());
        forEachBoard((board, icons) -> {
            Objective objective = board.getObjective(OBJECTIVE);
            if (objective == null) return;
            try {
                Score score = objective.getScore(player.getName());
                Score.class.getMethod("resetScore").invoke(score);
            } catch (ReflectiveOperationException | LinkageError ignored) {
                // older API: the blank score stays
            }
        });
    }

    /** After a prefix changed: the name tag teams of the icon board. */
    void syncTeams() {
        if (packBoard != null) Sidebar.copyTeams(main(), packBoard);
    }

    void disable() {
        if (!enabled) return;
        Objective objective = main().getObjective(OBJECTIVE);
        if (objective != null) objective.unregister();
    }

    private interface BoardAction {
        void run(Scoreboard board, boolean icons);
    }

    private void forEachBoard(BoardAction action) {
        action.run(main(), false);
        if (packBoard != null) action.run(packBoard, true);
        plugin.sidebar().forEachBoard((owner, board) -> action.run(board, plugin.glyphs().hasPack(owner)));
    }

    private Scoreboard packBoard() {
        if (packBoard == null) {
            packBoard = plugin.getServer().getScoreboardManager().getNewScoreboard();
            Sidebar.copyTeams(main(), packBoard);
            apply(packBoard, true);
        }
        return packBoard;
    }

    private Scoreboard main() {
        return plugin.getServer().getScoreboardManager().getMainScoreboard();
    }

    private void show(Scoreboard board, boolean icons, Player player) {
        Objective objective = objective(board);
        String line = lines.get(player.getUniqueId());
        try {
            Score score = objective.getScore(player.getName());
            score.setScore(0);
            Object format = line == null ? blank.invoke(null)
                    : fixed.invoke(null, deserialize.invoke(serializer, icons ? line : plugin.glyphs().fallback(line)));
            scoreFormat.invoke(score, format);
        } catch (ReflectiveOperationException | LinkageError e) {
            plugin.getLogger().warning("Could not show the line under " + player.getName() + ": " + e);
        }
    }

    private Objective objective(Scoreboard board) {
        Objective objective = board.getObjective(OBJECTIVE);
        if (objective != null) return objective;
        @SuppressWarnings("deprecation")  // the 1.13 API has no criteria enum yet
        Objective created = board.registerNewObjective(OBJECTIVE, "dummy");
        created.setDisplayName(" ");
        created.setDisplaySlot(DisplaySlot.BELOW_NAME);
        try {  // players without a line show nothing instead of "0"
            objectiveFormat.invoke(created, blank.invoke(null));
        } catch (ReflectiveOperationException | LinkageError ignored) {
            // checked in the constructor
        }
        return created;
    }
}
