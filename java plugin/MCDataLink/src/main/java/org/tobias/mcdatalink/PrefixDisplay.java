package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.scoreboard.Scoreboard;
import org.bukkit.scoreboard.Team;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Shows the prefixes from MCConnect in the chat, the tab list and above the head
 * (scoreboard team). Each place can be switched off in the config, e.g. when another
 * plugin already manages the tab list or name tags.
 */
final class PrefixDisplay {
    private static final String TEAM_PREFIX = "mcc";

    private final MCDataLink plugin;
    private final boolean chat;
    private final boolean tablist;
    private final boolean nametag;
    /** uuid -> formatted prefix, e.g. "§6[Bauteam]". Read by the async chat event. */
    private final Map<UUID, String> prefixes = new ConcurrentHashMap<>();

    PrefixDisplay(MCDataLink plugin, boolean chat, boolean tablist, boolean nametag) {
        this.plugin = plugin;
        this.chat = chat;
        this.tablist = tablist;
        this.nametag = nametag;
    }

    /** Formatted prefix, or null for an empty text. Unknown colors fall back to gray. */
    static String format(String color, String text) {
        if (text == null || text.trim().isEmpty()) return null;
        ChatColor chatColor;
        try {
            chatColor = ChatColor.valueOf(color.trim().toUpperCase());
        } catch (IllegalArgumentException | NullPointerException e) {
            chatColor = ChatColor.GRAY;
        }
        // strip formatting codes the website should never send anyway
        return chatColor + "[" + ChatColor.stripColor(text.replace('&', ' ')) + "]";
    }

    /** Called from the connection thread when MCConnect sends a prefix (empty text = remove). */
    void set(UUID uuid, String color, String text) {
        String formatted = format(color, text);
        if (formatted == null) prefixes.remove(uuid);
        else prefixes.put(uuid, formatted);
        plugin.runOnMainThread(() -> {
            Player player = plugin.getServer().getPlayer(uuid);
            if (player != null) apply(player);
        });
    }

    /** Chat format with the prefix in front, or the unchanged format. */
    String chatFormat(UUID uuid, String format) {
        String prefix = prefixes.get(uuid);
        if (!chat || prefix == null) return format;
        return prefix + ChatColor.RESET + " " + format;
    }

    /** Main thread: show the stored prefix (or none) for the player. */
    void apply(Player player) {
        String prefix = prefixes.get(player.getUniqueId());
        if (tablist) {
            player.setPlayerListName(prefix == null ? null : prefix + ChatColor.RESET + " " + player.getName());
        }
        if (nametag) {
            removeFromTeam(player);
            if (prefix != null) {
                Scoreboard board = plugin.getServer().getScoreboardManager().getMainScoreboard();
                String teamName = TEAM_PREFIX + Integer.toHexString(prefix.hashCode());
                Team team = board.getTeam(teamName);
                if (team == null) {
                    team = board.registerNewTeam(teamName);
                    team.setPrefix(prefix + ChatColor.RESET + " ");
                }
                team.addEntry(player.getName());
            }
        }
    }

    /** Main thread: when the player leaves, take them out of our name tag team. */
    void clear(Player player) {
        if (nametag) removeFromTeam(player);
    }

    private void removeFromTeam(Player player) {
        Scoreboard board = plugin.getServer().getScoreboardManager().getMainScoreboard();
        Team team = board.getEntryTeam(player.getName());
        if (team != null && team.getName().startsWith(TEAM_PREFIX)) {
            team.removeEntry(player.getName());
            if (team.getEntries().isEmpty()) team.unregister();
        }
    }
}
