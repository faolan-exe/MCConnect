package org.tobias.mcdatalink;

import org.bukkit.BanEntry;
import org.bukkit.BanList;
import org.bukkit.ChatColor;
import org.bukkit.entity.Player;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.UUID;

/**
 * Bans from the website go into the normal Minecraft ban list (source "MCConnect"),
 * so they are enforced even while MCConnect is not reachable. All other entries of
 * the ban list are reported to MCConnect so the website can show them.
 * All methods run on the main thread.
 */
final class BanSync {
    static final String SOURCE = "MCConnect";

    private final MCDataLink plugin;

    BanSync(MCDataLink plugin) {
        this.plugin = plugin;
    }

    @SuppressWarnings("deprecation")  // name bans are deprecated on newer servers but still supported
    private BanList banList() {
        return plugin.getServer().getBanList(BanList.Type.NAME);
    }

    /**
     * Returns whether the name is in the ban list now. A name ban needs the player's profile: on newer servers
     * addBan silently does nothing when the server cannot look the name up (Mojang not reachable), so MCConnect
     * sends the ban again later. The player is kicked in any case.
     */
    boolean ban(UUID uuid, String name, long endMillis, String reason, String untilText) {
        Date expires = endMillis > 0 ? new Date(endMillis) : null;
        boolean listed = false;
        try {
            banList().addBan(name, reason, expires, SOURCE);
            listed = banList().isBanned(name);
        } catch (Throwable t) {
            plugin.getLogger().severe("Could not ban " + name + ": " + t);
        }
        Player player = plugin.getServer().getPlayer(uuid);
        if (player != null) player.kickPlayer(kickMessage(reason, expires, untilText));
        if (listed) plugin.getLogger().info("Banned " + name + " (from MCConnect): " + reason);
        else plugin.getLogger().warning("Could not put " + name + " on the ban list, MCConnect tries again later");
        return listed;
    }

    /** Returns whether the name is off the ban list now. */
    boolean unban(String name) {
        try {
            banList().pardon(name);
            boolean free = !banList().isBanned(name);
            if (free) plugin.getLogger().info("Unbanned " + name + " (from MCConnect)");
            else plugin.getLogger().warning("Could not unban " + name + ", MCConnect tries again later");
            return free;
        } catch (Throwable t) {
            plugin.getLogger().severe("Could not unban " + name + ": " + t);
            return false;
        }
    }

    /** The ban list without MCConnect's own bans, as JSON for the !BANS message. */
    String banListJson() {
        StringBuilder json = new StringBuilder("[");
        try {
            for (Object object : banList().getBanEntries()) {
                BanEntry entry = (BanEntry) object;
                if (SOURCE.equals(entry.getSource())) continue;
                if (json.length() > 1) json.append(',');
                json.append("{\"name\":").append(quote(entry.getTarget()))
                    .append(",\"reason\":").append(quote(entry.getReason()))
                    .append(",\"source\":").append(quote(entry.getSource()))
                    .append(",\"created\":").append(entry.getCreated() == null ? 0 : entry.getCreated().getTime())
                    .append(",\"expires\":").append(entry.getExpiration() == null ? 0 : entry.getExpiration().getTime())
                    .append('}');
            }
        } catch (Throwable t) {
            plugin.getLogger().warning("Could not read the ban list: " + t);
            return null;
        }
        return json.append(']').toString();
    }

    /** Names of MCConnect's own bans still in the ban list (a missing one was lifted with /pardon). */
    String webBanNamesJson() {
        StringBuilder json = new StringBuilder("[");
        try {
            for (Object object : banList().getBanEntries()) {
                BanEntry entry = (BanEntry) object;
                if (!SOURCE.equals(entry.getSource())) continue;
                if (json.length() > 1) json.append(',');
                json.append(quote(entry.getTarget()));
            }
        } catch (Throwable t) {
            plugin.getLogger().warning("Could not read the ban list: " + t);
            return null;
        }
        return json.append(']').toString();
    }

    private static String kickMessage(String reason, Date expires, String untilText) {
        String until = untilText != null && !untilText.isEmpty() ? untilText
                : expires == null ? "dauerhaft" : "bis " + new SimpleDateFormat("dd.MM.yyyy HH:mm").format(expires);
        return ChatColor.RED + "Du wurdest gebannt" + ChatColor.RESET + "\n\n" + reason + "\n" + ChatColor.GRAY + until;
    }

    static String quote(String value) {
        if (value == null) return "null";
        StringBuilder out = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
            }
        }
        return out.append('"').toString();
    }
}
