package org.tobias.mcdatalink;

import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.messaging.PluginMessageListener;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Reports the connection and client of every online player to MCConnect once a minute (plugin 3.28, "clients"):
 * ping, client brand (vanilla, fabric, ...), client version (with ViaVersion), language, view distance and whether
 * the player plays Bedrock (with Floodgate). Only moderators see it on the website. No IP addresses.
 *
 * Everything newer than the 1.13 API is read by reflection, so older servers simply send fewer fields.
 */
final class ClientReporter implements Listener, PluginMessageListener {
    private static final long REPORT_INTERVAL_TICKS = 20L * 60;
    static final String BRAND_CHANNEL = "minecraft:brand";

    private final MCDataLink plugin;
    /** Brands sent by the clients on the brand channel (Spigot without getClientBrandName). */
    private final Map<UUID, String> brands = new ConcurrentHashMap<>();

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
        brands.remove(event.getPlayer().getUniqueId());
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
            String device = bedrockDevice(uuid);
            if (device != null) {
                json.append(",\"bedrock\":true");
                appendText(json, "device", device);
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

    /** "Android", "Windows", ... for Bedrock players with Floodgate; null for Java players or without Floodgate. */
    private String bedrockDevice(UUID uuid) {
        if (plugin.getServer().getPluginManager().getPlugin("floodgate") == null) return null;
        try {
            Object api = Class.forName("org.geysermc.floodgate.api.FloodgateApi").getMethod("getInstance").invoke(null);
            Object player = api.getClass().getMethod("getPlayer", UUID.class).invoke(api, uuid);
            if (player == null) return null;
            Object os = player.getClass().getMethod("getDeviceOs").invoke(player);
            return os == null ? "Bedrock" : os.toString();
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return null;
        }
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
