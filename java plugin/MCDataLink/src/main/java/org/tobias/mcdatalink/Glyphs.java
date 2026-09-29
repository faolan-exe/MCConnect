package org.tobias.mcdatalink;

import net.md_5.bungee.api.chat.BaseComponent;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerResourcePackStatusEvent;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The icons of MCConnect (characters U+E000... drawn by its resource pack, see database/glyphs.py). The pack is
 * offered to every player on join (!pack); players who declined it or have not loaded it yet see the Unicode
 * fallback of each icon instead, so chat lines, join messages and the line under the names never show empty boxes.
 */
final class Glyphs implements Listener {
    /** Fixed, so a newer pack replaces the older one on the client (1.20.3+ keeps several packs). */
    private static final UUID PACK_ID = UUID.nameUUIDFromBytes("mcconnect-icons".getBytes(StandardCharsets.UTF_8));
    private static final String PROMPT = "§7Icons von MCConnect für Join-Nachrichten und die Abzeichen unter den Namen."
            + " §8(freiwillig – ohne siehst du einfache Symbole)";
    private static final long OFFER_DELAY_TICKS = 20L;

    private final MCDataLink plugin;
    private final boolean offer;
    private volatile Map<Character, String> fallbacks = Collections.emptyMap();
    private volatile String url;
    private volatile String sha1;
    private volatile byte[] hash;
    /** Players whose client loaded the pack. */
    private final Set<UUID> loaded = ConcurrentHashMap.newKeySet();

    Glyphs(MCDataLink plugin, boolean offer) {
        this.plugin = plugin;
        this.offer = offer;
    }

    /** From the connection thread: !pack url|sha1|<icon><fallback>,... */
    void setPack(String url, String sha1, String map) {
        Map<Character, String> parsed = new HashMap<>();
        for (String pair : map.split(",")) {
            if (pair.length() >= 2) parsed.put(pair.charAt(0), pair.substring(1));
        }
        fallbacks = parsed;
        byte[] parsedHash = parseSha1(sha1);
        if (url.isEmpty() || parsedHash == null) return;
        boolean changed = !sha1.equalsIgnoreCase(String.valueOf(this.sha1));
        this.url = url;
        this.hash = parsedHash;
        this.sha1 = sha1;
        if (changed) {  // a newer pack (or the first one since the start): offer it to everyone online
            plugin.runOnMainThread(() -> {
                for (Player player : plugin.getServer().getOnlinePlayers()) offer(player);
            });
        }
    }

    boolean hasPack(UUID uuid) {
        return loaded.contains(uuid);
    }

    /** The text for this viewer: with the icons if their client loaded the pack, otherwise with the fallbacks. */
    String forViewer(String text, UUID viewer) {
        return viewer != null && loaded.contains(viewer) ? text : fallback(text);
    }

    String fallback(String text) {
        Map<Character, String> map = fallbacks;
        StringBuilder result = null;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c < 0xE000 || c > 0xF8FF) {  // outside the private use area
                if (result != null) result.append(c);
                continue;
            }
            if (result == null) result = new StringBuilder(text.length()).append(text, 0, i);
            String replacement = map.get(c);
            result.append(replacement != null ? replacement : "?");
        }
        return result == null ? text : result.toString();
    }

    /** A chat line (ChatMarkup) that is parsed at most once per variant, for sending it to many players. */
    Line line(String text) {
        return new Line(text);
    }

    final class Line {
        private final String text;
        private BaseComponent[] icons;
        private BaseComponent[] plain;

        private Line(String text) {
            this.text = text;
        }

        BaseComponent[] of(Player viewer) {
            if (loaded.contains(viewer.getUniqueId())) {
                if (icons == null) icons = ChatMarkup.parse(text);
                return icons;
            }
            if (plain == null) plain = ChatMarkup.parse(fallback(text));
            return plain;
        }
    }

    /** Main thread: offer the pack (1.20.3+: next to the server's own pack; older: instead of it). */
    private void offer(Player player) {
        String currentUrl = url;
        byte[] currentHash = hash;
        if (!offer || currentUrl == null || !player.isOnline()) return;
        try {
            Method add = Player.class.getMethod("addResourcePack", UUID.class, String.class, byte[].class,
                    String.class, boolean.class);
            add.invoke(player, PACK_ID, currentUrl, currentHash, PROMPT, false);
        } catch (ReflectiveOperationException | LinkageError e) {
            player.setResourcePack(currentUrl, currentHash);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        loaded.remove(player.getUniqueId());
        plugin.getServer().getScheduler().runTaskLater(plugin, () -> offer(player), OFFER_DELAY_TICKS);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        loaded.remove(event.getPlayer().getUniqueId());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPackStatus(PlayerResourcePackStatusEvent event) {
        if (!isOurPack(event)) return;
        Player player = event.getPlayer();
        String status = event.getStatus().name();
        boolean changed;
        if (status.equals("SUCCESSFULLY_LOADED")) {
            changed = loaded.add(player.getUniqueId());
        } else if (status.equals("ACCEPTED") || status.equals("DOWNLOADED")) {
            return;  // still loading
        } else {  // DECLINED, FAILED_DOWNLOAD, DISCARDED, INVALID_URL, FAILED_RELOAD
            changed = loaded.remove(player.getUniqueId());
        }
        if (changed) plugin.nameBadges().viewerChanged(player);
    }

    /** 1.20.3+ tells the packs apart (the server's own pack has another id); older servers only know one pack. */
    private static boolean isOurPack(PlayerResourcePackStatusEvent event) {
        try {
            return PACK_ID.equals(PlayerResourcePackStatusEvent.class.getMethod("getID").invoke(event));
        } catch (ReflectiveOperationException | LinkageError e) {
            return true;
        }
    }

    private static byte[] parseSha1(String hex) {
        if (hex == null || !hex.matches("[0-9a-fA-F]{40}")) return null;
        byte[] result = new byte[20];
        for (int i = 0; i < 20; i++) result[i] = (byte) Integer.parseInt(hex.substring(2 * i, 2 * i + 2), 16);
        return result;
    }
}
