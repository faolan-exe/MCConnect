package org.tobias.mcdatalink;

import net.md_5.bungee.api.chat.BaseComponent;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

import java.util.Collections;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Own join and leave messages of players (chosen on the website or with /joinmessage, unlocked by reward
 * levels) with an optional sound for everyone. Players can mute the messages of others and all join sounds;
 * a muted message is simply not sent to them. Players without an own message keep the game's message.
 */
final class JoinMessages implements Listener {
    private static final class Style {
        final String join;
        final String leave;
        final String sound;
        final float volume;

        Style(String join, String leave, String sound, float volume) {
            this.join = join;
            this.leave = leave;
            this.sound = sound;
            this.volume = volume;
        }
    }

    private final MCDataLink plugin;
    private final Map<UUID, Style> styles = new ConcurrentHashMap<>();
    /** player -> the players whose messages they do not see */
    private final Map<UUID, Set<UUID>> muted = new ConcurrentHashMap<>();
    private final Set<UUID> soundsOff = ConcurrentHashMap.newKeySet();

    JoinMessages(MCDataLink plugin) {
        this.plugin = plugin;
    }

    /** From the connection thread: !joinstyle (empty join line: the game's own message). */
    void setStyle(UUID uuid, String join, String leave, String sound, String volume) {
        if (join.isEmpty() && leave.isEmpty()) {
            styles.remove(uuid);
            return;
        }
        float level;
        try {
            level = Float.parseFloat(volume);
        } catch (NumberFormatException e) {
            level = 0.5f;
        }
        styles.put(uuid, new Style(join, leave, sound, Math.max(0f, Math.min(1f, level))));
    }

    /** From the connection thread: !joinmutes. */
    void setMutes(UUID uuid, boolean noSounds, Set<UUID> mutedPlayers) {
        if (noSounds) soundsOff.add(uuid);
        else soundsOff.remove(uuid);
        if (mutedPlayers.isEmpty()) muted.remove(uuid);
        else muted.put(uuid, mutedPlayers);
    }

    /** From the connection thread: !joinreset before a full sync. */
    void reset() {
        styles.clear();
        muted.clear();
        soundsOff.clear();
    }

    static Set<UUID> parseUuids(String list) {
        if (list == null || list.isEmpty()) return Collections.emptySet();
        Set<UUID> result = new HashSet<>();
        for (String part : list.split(",")) {
            try {
                result.add(UUID.fromString(part.trim()));
            } catch (IllegalArgumentException ignored) {
                // skip broken entries
            }
        }
        return result;
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onJoin(PlayerJoinEvent event) {
        Style style = styles.get(event.getPlayer().getUniqueId());
        if (style == null || style.join.isEmpty()) return;
        event.setJoinMessage(null);
        announce(event.getPlayer(), style.join, style, true);
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onQuit(PlayerQuitEvent event) {
        Style style = styles.get(event.getPlayer().getUniqueId());
        if (style == null || style.leave.isEmpty()) return;
        event.setQuitMessage(null);
        announce(event.getPlayer(), style.leave, style, false);
    }

    private void announce(Player subject, String line, Style style, boolean withSound) {
        BaseComponent[] message = ChatMarkup.parse(line);
        UUID uuid = subject.getUniqueId();
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            if (!player.getUniqueId().equals(uuid) && muted.getOrDefault(player.getUniqueId(), Collections.emptySet()).contains(uuid)) {
                continue;
            }
            player.spigot().sendMessage(message);
            if (withSound && !style.sound.isEmpty() && !soundsOff.contains(player.getUniqueId())) {
                player.playSound(player.getLocation(), style.sound, style.volume, 1f);
            }
        }
        plugin.getLogger().info(ChatMarkup.plain(line));
    }
}
