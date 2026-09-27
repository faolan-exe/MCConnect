package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.AsyncPlayerChatEvent;
import org.bukkit.event.player.PlayerCommandPreprocessEvent;
import org.bukkit.event.player.PlayerLoginEvent;

import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;

/**
 * Mutes from MCConnect (chat and private messages are blocked until the end), the whitelist of
 * accepted applications / invite codes and the link to the application page in the kick message
 * of players who are not on the whitelist.
 */
final class Moderation implements Listener {
    /** Commands that send messages to other players and are blocked while muted. */
    private static final Set<String> MESSAGE_COMMANDS = new HashSet<>(Arrays.asList(
            "msg", "tell", "w", "whisper", "me", "r", "reply", "m", "t", "pm", "dm", "teammsg", "tm"));
    private static final Pattern NAME = Pattern.compile("^[A-Za-z0-9_]{3,16}$");

    private final MCDataLink plugin;
    /** uuid -> end of the mute (epoch millis) and reason */
    private final Map<UUID, Long> mutedUntil = new ConcurrentHashMap<>();
    private final Map<UUID, String> muteReasons = new ConcurrentHashMap<>();
    /** "bis 27.09. 19:14 Uhr" from MCConnect (local time zone; this JVM may run in UTC) */
    private final Map<UUID, String> muteUntilTexts = new ConcurrentHashMap<>();
    private volatile String joinUrl = "";

    Moderation(MCDataLink plugin) {
        this.plugin = plugin;
    }

    /** From the connection thread: until 0 = unmuted. untilText may be empty (older MCConnect). */
    void setMute(UUID uuid, long until, String reason, String untilText) {
        if (until <= System.currentTimeMillis()) {
            mutedUntil.remove(uuid);
            muteReasons.remove(uuid);
            muteUntilTexts.remove(uuid);
        } else {
            mutedUntil.put(uuid, until);
            muteReasons.put(uuid, reason == null ? "" : reason);
            muteUntilTexts.put(uuid, untilText == null ? "" : untilText);
        }
    }

    void setJoinUrl(String url) {
        joinUrl = url == null ? "" : url.trim();
    }

    /** Main thread: put an accepted player on the whitelist. */
    void whitelist(String name) {
        if (!NAME.matcher(name).matches()) return;
        plugin.getServer().dispatchCommand(plugin.getServer().getConsoleSender(), "whitelist add " + name);
    }

    private String muteMessage(UUID uuid) {
        Long until = mutedUntil.get(uuid);
        if (until == null) return null;
        if (until <= System.currentTimeMillis()) {
            mutedUntil.remove(uuid);
            muteReasons.remove(uuid);
            muteUntilTexts.remove(uuid);
            return null;
        }
        String reason = muteReasons.getOrDefault(uuid, "");
        String untilText = muteUntilTexts.getOrDefault(uuid, "");
        if (untilText.isEmpty()) untilText = "bis " + new SimpleDateFormat("dd.MM. HH:mm").format(new Date(until)) + " Uhr";
        return ChatColor.RED + "Du bist stummgeschaltet " + untilText + (reason.isEmpty() ? "." : ": " + ChatColor.WHITE + reason);
    }

    @EventHandler(priority = EventPriority.LOWEST, ignoreCancelled = true)
    @SuppressWarnings("deprecation")  // still fired by Paper for plugins that listen to it
    public void onChat(AsyncPlayerChatEvent event) {
        String message = muteMessage(event.getPlayer().getUniqueId());
        if (message != null) {
            event.setCancelled(true);
            event.getPlayer().sendMessage(message);
        }
    }

    @EventHandler(priority = EventPriority.LOWEST, ignoreCancelled = true)
    public void onCommand(PlayerCommandPreprocessEvent event) {
        String command = event.getMessage().substring(1).split(" ", 2)[0].toLowerCase();
        if (command.contains(":")) command = command.substring(command.indexOf(':') + 1);
        if (!MESSAGE_COMMANDS.contains(command)) return;
        String message = muteMessage(event.getPlayer().getUniqueId());
        if (message != null) {
            event.setCancelled(true);
            event.getPlayer().sendMessage(message);
        }
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onLogin(PlayerLoginEvent event) {
        if (event.getResult() == PlayerLoginEvent.Result.KICK_WHITELIST && !joinUrl.isEmpty()) {
            event.setKickMessage(ChatColor.GOLD + "Du stehst noch nicht auf der Whitelist.\n" + ChatColor.GRAY
                    + "Bewirb dich oder löse einen Einladungscode ein:\n" + ChatColor.AQUA + joinUrl);
        }
    }
}
