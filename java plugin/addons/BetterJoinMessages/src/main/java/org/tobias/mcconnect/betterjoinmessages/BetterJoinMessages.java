package org.tobias.mcconnect.betterjoinmessages;

import org.bukkit.ChatColor;
import org.bukkit.OfflinePlayer;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.tobias.mcdatalink.api.Addon;
import org.tobias.mcdatalink.api.AddonConfig;
import org.tobias.mcdatalink.api.AddonHandle;
import org.tobias.mcdatalink.api.MCConnect;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Join and leave messages of MCConnect as an addon (they were part of MCDataLink up to 3.21).
 *
 * MCConnect sends, through the addon channel, the same messages MCDataLink used to get: !joinstyle (a player's own
 * lines and sound, chosen on the website or with /joinmessage, unlocked by reward levels), !joinmutes (whose messages
 * a player does not see, sounds off), !joindefault (the lines of players without an own choice, "{name}") and
 * !joinreset. On top of that the addon's settings: a message for brand-new players, a greeting, fixed lines for single
 * players, silent players and no message for a quick reconnect. While the addon is not active the game's own
 * messages stay.
 */
public final class BetterJoinMessages extends JavaPlugin implements Addon, Listener {
    private static final List<String> FIELDS = Arrays.asList("text", "leave", "farbe", "symbol", "stil", "sound");

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

    private AddonHandle handle;
    private AddonConfig settings;
    private final Map<UUID, Style> styles = new HashMap<>();
    /** player -> the players whose messages they do not see */
    private final Map<UUID, Set<UUID>> muted = new HashMap<>();
    private final Set<UUID> soundsOff = new HashSet<>();
    /** Lines of players without an own style, "{name}" is replaced; null: the game's own message. */
    private Style defaultStyle;
    /** lower case name -> fixed lines from the settings ("overrides") */
    private final Map<String, Style> fixed = new HashMap<>();
    private final Map<UUID, Long> leftAt = new HashMap<>();

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). BetterJoinMessages needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(this, this);
        PluginCommand command = getCommand("joinmessage");
        if (command != null) {
            command.setExecutor(this);
            command.setTabCompleter(this);
        }
    }

    // ------------------------------------------------------------------ MCConnect

    @Override
    public void onActivate(AddonConfig config) {
        settings = config;
        fixed.clear();
        for (AddonConfig row : config.getList("overrides")) {
            String name = row.getString("player", "").trim().toLowerCase(Locale.ROOT);
            if (!name.isEmpty()) fixed.put(name, new Style(row.getString("join", ""), row.getString("leave", ""), "", 0f));
        }
    }

    @Override
    public void onDeactivate() {
        settings = null;
        styles.clear();
        muted.clear();
        soundsOff.clear();
        defaultStyle = null;
        fixed.clear();
    }

    @Override
    public void onMessage(String message) {
        int separator = message.indexOf('~');
        String command = separator < 0 ? message : message.substring(0, separator);
        String[] fields = (separator < 0 ? "" : message.substring(separator + 1)).split("\\|", -1);
        switch (command) {
            case "!joinstyle": {  // uuid|join line|leave line|sound|volume (empty lines: the default lines)
                UUID uuid = fields.length >= 5 ? parseUuid(fields[0]) : null;
                if (uuid == null) break;
                if (fields[1].isEmpty() && fields[2].isEmpty()) styles.remove(uuid);
                else styles.put(uuid, new Style(fields[1], fields[2], fields[3], parseVolume(fields[4])));
                break;
            }
            case "!joinmutes": {  // uuid|sounds off 0/1|uuid,uuid,...
                UUID uuid = fields.length >= 3 ? parseUuid(fields[0]) : null;
                if (uuid == null) break;
                if ("1".equals(fields[1])) soundsOff.add(uuid);
                else soundsOff.remove(uuid);
                Set<UUID> list = parseUuids(fields[2]);
                if (list.isEmpty()) muted.remove(uuid);
                else muted.put(uuid, list);
                break;
            }
            case "!joindefault":  // join line|leave line with {name}
                if (fields.length >= 2) defaultStyle = fields[0].isEmpty() && fields[1].isEmpty() ? null : new Style(fields[0], fields[1], "", 0f);
                break;
            case "!joinreset":
                styles.clear();
                muted.clear();
                soundsOff.clear();
                defaultStyle = null;
                break;
            default:
                break;
        }
    }

    // ------------------------------------------------------------------ events

    @EventHandler(priority = EventPriority.HIGH)
    public void onJoin(PlayerJoinEvent event) {
        AddonConfig config = settings;
        if (config == null) return;
        Player player = event.getPlayer();
        Long left = leftAt.remove(player.getUniqueId());
        int rejoin = config.getInt("rejoin_seconds", 30);
        boolean quickReturn = left != null && rejoin > 0 && System.currentTimeMillis() - left < rejoin * 1000L;
        if (isSilent(player, config) || quickReturn) {
            event.setJoinMessage(null);
        } else if (!player.hasPlayedBefore() && config.getBoolean("first_join_enabled", true)) {
            event.setJoinMessage(null);
            String line = config.getString("first_join_text", "").replace("{name}", player.getName())
                        .replace("{count}", String.valueOf(playerCount()));
            announce(player, line, config.getString("first_join_sound", ""), 0.6f, true);
        } else {
            Style style = styleOf(player);
            if (style != null) {
                event.setJoinMessage(null);  // an empty line (fixed lines): silent
                if (!style.join.isEmpty()) announce(player, style.join, style.sound, style.volume, false);
            }
        }
        greet(player, config);
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onQuit(PlayerQuitEvent event) {
        AddonConfig config = settings;
        if (config == null) return;
        Player player = event.getPlayer();
        leftAt.put(player.getUniqueId(), System.currentTimeMillis());
        if (isSilent(player, config)) {
            event.setQuitMessage(null);
            return;
        }
        Style style = styleOf(player);
        if (style == null) return;
        event.setQuitMessage(null);
        if (!style.leave.isEmpty()) announce(player, style.leave, "", 0f, false);
    }

    private boolean isSilent(Player player, AddonConfig config) {
        return config.getBoolean("silent_permission", true) && player.hasPermission("betterjoinmessages.silent");
    }

    /** Fixed lines from the settings, the player's own style, the default lines, or null (the game's message). */
    private Style styleOf(Player player) {
        Style own = fixed.get(player.getName().toLowerCase(Locale.ROOT));
        if (own == null) own = styles.get(player.getUniqueId());
        if (own == null) own = defaultStyle;
        if (own == null) return null;
        return new Style(own.join.replace("{name}", player.getName()), own.leave.replace("{name}", player.getName()),
                own.sound, own.volume);
    }

    private void announce(Player subject, String line, String sound, float volume, boolean everyoneHears) {
        UUID uuid = subject.getUniqueId();
        org.tobias.mcdatalink.api.ChatLine message = handle.chat(line);  // icons only for players with the resource pack
        for (Player player : getServer().getOnlinePlayers()) {
            if (!player.getUniqueId().equals(uuid) && muted.getOrDefault(player.getUniqueId(), Collections.emptySet()).contains(uuid)) {
                continue;
            }
            message.send(player);
            if (!sound.isEmpty() && (everyoneHears || !soundsOff.contains(player.getUniqueId()))) {
                player.playSound(player.getLocation(), sound, volume, 1f);
            }
        }
        if (settings != null && settings.getBoolean("log_console", true)) getLogger().info(handle.plain(line));
    }

    /** The greeting in the chat and the title, a moment after the join (after the join message). */
    private void greet(Player player, AddonConfig config) {
        boolean chat = config.getBoolean("welcome_enabled", false);
        boolean title = config.getBoolean("title_enabled", false);
        if (!chat && !title) return;
        getServer().getScheduler().runTaskLater(this, () -> {
            if (!player.isOnline()) return;
            if (chat) {
                for (String line : config.getStringList("welcome_lines")) {
                    handle.chat(line.replace("{name}", player.getName())
                            .replace("{online}", String.valueOf(getServer().getOnlinePlayers().size()))
                            .replace("{max}", String.valueOf(getServer().getMaxPlayers()))).send(player);
                }
            }
            if (title) {
                player.sendTitle(color(config.getString("title", "").replace("{name}", player.getName())),
                        color(config.getString("subtitle", "").replace("{name}", player.getName())), 10, 60, 20);
            }
        }, 10L);
    }

    private int playerCount() {
        // the game writes the new player's data file later, so they are not counted yet
        OfflinePlayer[] players = getServer().getOfflinePlayers();
        return players.length + 1;
    }

    // ------------------------------------------------------------------ /joinmessage

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player)) {
            sender.sendMessage("Dieser Befehl geht nur im Spiel.");
            return true;
        }
        if (!handle.isActive()) {
            sender.sendMessage(ChatColor.GRAY + "Join-Nachrichten sind auf diesem Server gerade ausgeschaltet.");
        } else if (!handle.command((Player) sender, "joinmessage", String.join(" ", args))) {
            sender.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar. Versuche es gleich noch einmal.");
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length != 1) return Collections.emptyList();
        List<String> matches = new ArrayList<>();
        for (String field : FIELDS) {
            if (field.startsWith(args[0].toLowerCase(Locale.ROOT))) matches.add(field);
        }
        return matches;
    }

    // ------------------------------------------------------------------ helpers

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }

    private static float parseVolume(String value) {
        try {
            return Math.max(0f, Math.min(1f, Float.parseFloat(value)));
        } catch (NumberFormatException e) {
            return 0.5f;
        }
    }

    private static UUID parseUuid(String value) {
        try {
            return UUID.fromString(value.trim());
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static Set<UUID> parseUuids(String list) {
        Set<UUID> result = new HashSet<>();
        for (String part : list.split(",")) {
            UUID uuid = parseUuid(part);
            if (uuid != null) result.add(uuid);
        }
        return result;
    }
}
