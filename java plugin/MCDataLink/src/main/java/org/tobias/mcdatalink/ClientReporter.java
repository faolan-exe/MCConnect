package org.tobias.mcdatalink;

import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.EventPriority;
import org.bukkit.event.player.PlayerLoginEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerResourcePackStatusEvent;
import org.bukkit.plugin.messaging.PluginMessageListener;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Reports the connection and client of every online player to MCConnect once a minute (plugin 3.28, "clients"):
 * ping, client brand (vanilla, fabric, ...), client version (with ViaVersion), language, view distance and whether
 * the player plays Bedrock (with Floodgate: device, input, UI and version). Since 3.29 also the plugin channels the
 * client registered (they show its mods), the status of the resource pack, the client options (chat, main hand,
 * particles, ... on Paper) and the address the player joined with. Since 3.30 the click patterns and the rhythm of
 * the client (see InputRhythm). Only moderators see it. No IP addresses.
 *
 * Everything newer than the 1.13 API is read by reflection, so older servers simply send fewer fields.
 */
final class ClientReporter implements Listener, PluginMessageListener {
    private static final long REPORT_INTERVAL_TICKS = 20L * 60;
    static final String BRAND_CHANNEL = "minecraft:brand";

    private final MCDataLink plugin;
    /** Brands sent by the clients on the brand channel (Spigot without getClientBrandName). */
    private final Map<UUID, String> brands = new ConcurrentHashMap<>();
    /** The address each player joined with (host name without port). */
    private final Map<UUID, String> hosts = new ConcurrentHashMap<>();
    /** The last status of a resource pack (MCConnect's or the server's own). */
    private final Map<UUID, String> packs = new ConcurrentHashMap<>();
    private static final String[][] CLIENT_OPTIONS = {  // Paper's ClientOption fields -> JSON keys
            {"CHAT_VISIBILITY", "chat"}, {"CHAT_COLORS_ENABLED", "chat_colors"}, {"TEXT_FILTERING_ENABLED", "text_filter"},
            {"ALLOW_SERVER_LISTINGS", "listing"}, {"PARTICLE_VISIBILITY", "particles"}, {"SKIN_PARTS", "skin"}};

    ClientReporter(MCDataLink plugin) {
        this.plugin = plugin;
    }

    void start() {
        try {
            plugin.getServer().getMessenger().registerIncomingPluginChannel(plugin, BRAND_CHANNEL, this);
        } catch (RuntimeException e) {  // the server does not allow listening to this channel
            plugin.getLogger().fine("Client brands only from the API: " + e.getMessage());
        }
        plugin.getServer().getPluginManager().registerEvents(this, plugin);
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::report, REPORT_INTERVAL_TICKS, REPORT_INTERVAL_TICKS);
    }

    @Override
    public void onPluginMessageReceived(String channel, Player player, byte[] message) {
        if (!BRAND_CHANNEL.equals(channel) || message.length < 2) return;
        // a string with a VarInt length in front (the brand is short: one byte of length)
        int length = message[0] & 0x7F;
        if ((message[0] & 0x80) != 0 || length > message.length - 1) return;
        brands.put(player.getUniqueId(), new String(message, 1, length, StandardCharsets.UTF_8));
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        UUID uuid = event.getPlayer().getUniqueId();
        brands.remove(uuid);
        hosts.remove(uuid);
        packs.remove(uuid);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onLogin(PlayerLoginEvent event) {
        String host = event.getHostname();
        if (host == null || host.isEmpty()) return;
        int colon = host.lastIndexOf(':');
        if (colon > 0 && host.indexOf(':') == colon) host = host.substring(0, colon);  // drop the port (not of IPv6)
        // Forge and some proxies append markers after a NUL character
        int nul = host.indexOf('\0');
        if (nul >= 0) host = host.substring(0, nul);
        hosts.put(event.getPlayer().getUniqueId(), host.toLowerCase(Locale.ROOT));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPack(PlayerResourcePackStatusEvent event) {
        packs.put(event.getPlayer().getUniqueId(), event.getStatus().name().toLowerCase(Locale.ROOT));
    }

    /** Main thread: average ping of the online players, or -1 without any (for the health sample). */
    int averagePing() {
        int total = 0;
        int count = 0;
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            int ping = ping(player);
            if (ping >= 0) {
                total += ping;
                count++;
            }
        }
        return count == 0 ? -1 : Math.round(total / (float) count);
    }

    /** Main thread: median ping of the online players, or -1 without any (3.30; one bad connection does not move it). */
    int medianPing() {
        java.util.List<Integer> pings = new java.util.ArrayList<>();
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            int ping = ping(player);
            if (ping >= 0) pings.add(ping);
        }
        if (pings.isEmpty()) return -1;
        java.util.Collections.sort(pings);
        int middle = pings.size() / 2;
        return pings.size() % 2 == 1 ? pings.get(middle) : Math.round((pings.get(middle - 1) + pings.get(middle)) / 2f);
    }

    /** Main thread: one entry per online player. */
    void report() {
        if (plugin.getServer().getOnlinePlayers().isEmpty()) return;
        StringBuilder json = new StringBuilder("[");
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            if (json.length() > 1) json.append(',');
            UUID uuid = player.getUniqueId();
            json.append("{\"uuid\":\"").append(uuid).append('"');
            int ping = ping(player);
            if (ping >= 0) json.append(",\"ping\":").append(ping);
            appendText(json, "brand", brand(player));
            appendText(json, "version", clientVersion(uuid));
            appendText(json, "locale", player.getLocale());
            Object view = call(player, "getClientViewDistance");
            if (view instanceof Integer) json.append(",\"view\":").append(view);
            Object floodgate = floodgatePlayer(uuid);
            if (floodgate != null) {
                json.append(",\"bedrock\":true");
                appendText(json, "device", text(call(floodgate, "getDeviceOs")));
                appendText(json, "input", text(call(floodgate, "getInputMode")));
                appendText(json, "ui", text(call(floodgate, "getUiProfile")));
                appendText(json, "bedrock_version", text(call(floodgate, "getVersion")));
            }
            appendText(json, "host", hosts.get(uuid));
            appendText(json, "pack", packs.get(uuid));
            Object hand = call(player, "getMainHand");
            appendText(json, "hand", text(hand));
            appendClientOptions(json, player);
            appendChannels(json, player);
            InputRhythm rhythm = plugin.inputRhythm();
            if (rhythm != null) {
                String timing = rhythm.report(uuid);  // drained even for Bedrock, so the counters stay per minute
                if (floodgate == null) json.append(timing);  // Geyser makes up the packets of Bedrock players
            }
            json.append('}');
        }
        plugin.sendClients(json.append(']').toString());
    }

    /** Milliseconds, or -1 if the server cannot tell: Player.getPing() since 1.17, the NMS field before. */
    static int ping(Player player) {
        Object ping = call(player, "getPing");
        if (ping instanceof Integer) return (Integer) ping;
        try {
            Object handle = player.getClass().getMethod("getHandle").invoke(player);
            for (String name : new String[]{"ping", "latency", "e"}) {
                try {
                    Field field = handle.getClass().getField(name);
                    if (field.getType() == int.class) return field.getInt(handle);
                } catch (NoSuchFieldException ignored) {
                    // next name
                }
            }
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // no CraftBukkit
        }
        return -1;
    }

    private String brand(Player player) {
        Object brand = call(player, "getClientBrandName");  // Paper 1.19.4+
        if (brand instanceof String) return (String) brand;
        return brands.get(player.getUniqueId());
    }

    /** The client's Minecraft version with ViaVersion installed ("1.21.4"), otherwise null (= the server's version). */
    private String clientVersion(UUID uuid) {
        if (plugin.getServer().getPluginManager().getPlugin("ViaVersion") == null) return null;
        try {
            Object api = Class.forName("com.viaversion.viaversion.api.Via").getMethod("getAPI").invoke(null);
            Object protocol = api.getClass().getMethod("getPlayerVersion", UUID.class).invoke(api, uuid);
            Object version = Class.forName("com.viaversion.viaversion.api.protocol.version.ProtocolVersion")
                    .getMethod("getProtocol", int.class).invoke(null, protocol);
            Object name = version.getClass().getMethod("getName").invoke(version);
            return name instanceof String ? (String) name : null;
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return null;
        }
    }

    /** Floodgate's player object of a Bedrock player, or null (Java player or no Floodgate). */
    private Object floodgatePlayer(UUID uuid) {
        if (plugin.getServer().getPluginManager().getPlugin("floodgate") == null) return null;
        try {
            Object api = Class.forName("org.geysermc.floodgate.api.FloodgateApi").getMethod("getInstance").invoke(null);
            return api.getClass().getMethod("getPlayer", UUID.class).invoke(api, uuid);
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return null;
        }
    }

    /** Paper's client options (1.20.1+, particles 1.21+): chat visibility, chat colors, text filter, server listings,
     *  particles and the shown skin parts (cape and the count of the outer layers). */
    private static void appendClientOptions(StringBuilder json, Player player) {
        Class<?> options;
        Method get;
        try {
            options = Class.forName("com.destroystokyo.paper.ClientOption");
            get = player.getClass().getMethod("getClientOption", options);
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return;  // Spigot
        }
        for (String[] option : CLIENT_OPTIONS) {
            try {
                Object value = get.invoke(player, options.getField(option[0]).get(null));
                if (value == null) continue;
                if (option[1].equals("skin")) {
                    Object cape = call(value, "hasCapeEnabled");
                    if (cape instanceof Boolean) json.append(",\"cape\":").append(cape);
                    Object raw = call(value, "getRaw");
                    if (raw instanceof Integer) json.append(",\"skin_layers\":").append(Integer.bitCount((Integer) raw & 0x7F));
                } else if (value instanceof Boolean) {
                    json.append(",\"").append(option[1]).append("\":").append(value);
                } else {
                    appendText(json, option[1], text(value));
                }
            } catch (ReflectiveOperationException | RuntimeException | LinkageError ignored) {
                // this option is newer than the server
            }
        }
    }

    /** The plugin channels the client registered: mods announce themselves there (voice chat, minimaps, ...). */
    private static void appendChannels(StringBuilder json, Player player) {
        Set<String> channels;
        try {
            channels = player.getListeningPluginChannels();
        } catch (RuntimeException e) {
            return;
        }
        StringBuilder list = new StringBuilder();
        int count = 0;
        for (String channel : channels) {
            if (count++ >= 60) break;
            String clean = channel.replaceAll("[^a-z0-9_.:/-]", "");
            if (clean.isEmpty() || clean.length() > 64) continue;
            if (list.length() > 0) list.append(',');
            list.append('"').append(clean).append('"');
        }
        json.append(",\"channels\":[").append(list).append(']');
    }

    private static String text(Object value) {
        return value == null ? null : value.toString();
    }

    private static Object call(Object target, String method) {
        try {
            Method m = target.getClass().getMethod(method);
            return m.invoke(target);
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return null;
        }
    }

    private static void appendText(StringBuilder json, String key, String value) {
        if (value == null || value.isEmpty()) return;
        String clean = value.replaceAll("[\"\\\\\\p{Cntrl}]", "");
        if (clean.length() > 48) clean = clean.substring(0, 48);
        json.append(",\"").append(key).append("\":\"").append(clean).append('"');
    }
}
