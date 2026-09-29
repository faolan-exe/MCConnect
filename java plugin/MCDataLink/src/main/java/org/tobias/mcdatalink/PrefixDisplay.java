package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.event.player.AsyncPlayerChatEvent;
import org.bukkit.scoreboard.Scoreboard;
import org.bukkit.scoreboard.Team;

import java.util.IllegalFormatException;
import java.util.Iterator;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Shows the prefixes from MCConnect in the chat, the tab list and above the head
 * (scoreboard team). Each place can be switched off in the config, e.g. when another
 * plugin already manages the tab list or name tags.
 *
 * Prefixes may contain the server's own icons (Glyphs, plugin 3.19). Players whose client loaded the resource pack see
 * them above the heads (their scoreboard, see NameBadges) and in the chat (sent to them separately); everyone else sees
 * the fallback characters. The tab list is the same for everyone, so it always shows the fallbacks.
 */
final class PrefixDisplay {
    private static final String TEAM_PREFIX = "mcc";

    private final MCDataLink plugin;
    private final boolean chat;
    private final boolean tablist;
    private final boolean nametag;
    /** uuid -> formatted prefix with icons, e.g. "§6[Bauteam]". Read by the async chat event. */
    private final Map<UUID, String> prefixes = new ConcurrentHashMap<>();
    /** name tag team -> its prefix with icons (the main scoreboard has the fallback variant) */
    private final Map<String, String> teamPrefixes = new ConcurrentHashMap<>();

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

    boolean nametag() {
        return nametag;
    }

    /** Chat format with the prefix (fallback variant) in front, or the unchanged format. */
    String chatFormat(UUID uuid, String format) {
        String prefix = prefixes.get(uuid);
        if (!chat || prefix == null) return format;
        return plugin.glyphs().fallback(prefix) + ChatColor.RESET + " " + format;
    }

    /**
     * After every other plugin (MONITOR): players with the resource pack get the line with the icons of the prefix
     * instead, as a message of their own; the event then goes to the others only.
     */
    void sendIcons(AsyncPlayerChatEvent event) {
        String prefix = prefixes.get(event.getPlayer().getUniqueId());
        if (!chat || prefix == null) return;
        String plain = plugin.glyphs().fallback(prefix);
        String format = event.getFormat();
        int at = format.indexOf(plain);
        if (plain.equals(prefix) || at < 0) return;
        String line;
        try {
            line = String.format(format.substring(0, at) + prefix + format.substring(at + plain.length()),
                    event.getPlayer().getDisplayName(), event.getMessage());
        } catch (IllegalFormatException e) {
            return;
        }
        try {
            for (Iterator<Player> it = event.getRecipients().iterator(); it.hasNext(); ) {
                Player viewer = it.next();
                if (!plugin.glyphs().hasPack(viewer.getUniqueId())) continue;
                it.remove();
                viewer.sendMessage(line);
            }
        } catch (UnsupportedOperationException ignored) {
            // recipients cannot be changed on this server: everyone keeps the fallbacks
        }
    }

    /** MCConnect's name tag teams of the main scoreboard into another board, with icons or fallbacks. */
    void copyTeams(Scoreboard board, boolean icons) {
        Scoreboard main = plugin.getServer().getScoreboardManager().getMainScoreboard();
        for (Team team : board.getTeams()) {
            if (team.getName().startsWith(TEAM_PREFIX) && main.getTeam(team.getName()) == null) team.unregister();
        }
        for (Team source : main.getTeams()) {
            if (!source.getName().startsWith(TEAM_PREFIX)) continue;
            Team copy = board.getTeam(source.getName());
            if (copy == null) copy = board.registerNewTeam(source.getName());
            String withIcons = teamPrefixes.get(source.getName());
            String prefix = icons && withIcons != null ? withIcons + ChatColor.RESET + " " : source.getPrefix();
            if (!prefix.equals(copy.getPrefix())) copy.setPrefix(prefix);
            for (String entry : copy.getEntries()) {
                if (!source.hasEntry(entry)) copy.removeEntry(entry);
            }
            for (String entry : source.getEntries()) {
                if (!copy.hasEntry(entry)) copy.addEntry(entry);
            }
        }
    }

    /** Main thread: show the stored prefix (or none) for the player. */
    void apply(Player player) {
        String prefix = prefixes.get(player.getUniqueId());
        if (tablist) {
            player.setPlayerListName(prefix == null ? null
                    : plugin.glyphs().fallback(prefix) + ChatColor.RESET + " " + player.getName());
        }
        if (nametag) {
            removeFromTeam(player);
            if (prefix != null) {
                Scoreboard board = plugin.getServer().getScoreboardManager().getMainScoreboard();
                String teamName = TEAM_PREFIX + Integer.toHexString(prefix.hashCode());
                Team team = board.getTeam(teamName);
                if (team == null) team = board.registerNewTeam(teamName);
                String plain = plugin.glyphs().fallback(prefix) + ChatColor.RESET + " ";
                if (!plain.equals(team.getPrefix())) team.setPrefix(plain);  // the fallbacks may have changed
                teamPrefixes.put(teamName, prefix);
                team.addEntry(player.getName());
            }
            if (plugin.sidebar() != null) plugin.sidebar().syncTeams();
            if (plugin.nameBadges() != null) plugin.nameBadges().syncTeams();
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
            if (team.getEntries().isEmpty()) {
                teamPrefixes.remove(team.getName());
                team.unregister();
            }
        }
    }
}
