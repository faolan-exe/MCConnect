package org.tobias.mcdatalink;

import io.netty.channel.Channel;
import io.netty.channel.ChannelDuplexHandler;
import io.netty.channel.ChannelHandlerContext;
import io.netty.channel.ChannelPipeline;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Click patterns and the rhythm of the client (plugin 3.30, ADR 0013), measured where the packets arrive: a small
 * handler in the player's Netty pipeline notes the time of every packet in milliseconds (Bukkit events only know the
 * server tick, 50 ms).
 *
 * Clicks: every arm swing that is not part of mining (the client swings every tick while it breaks a block) or of a
 * right click (using an item swings too). Intervals below BURST_MS count as clicking in a row; their sum, the sum of
 * their squares and a histogram give the speed and how even the clicks are. An auto clicker is unnaturally even.
 *
 * Rhythm: the longest silence between two packets and the silences above a threshold ("stalls"). Clients since
 * 1.21.2 send a packet at the end of every tick, so a silence above 250 ms means the client froze or the connection
 * hung; older clients only send their position at least once a second when they stand still (threshold 1.5 s).
 * Frames per second cannot be measured: the client sends per tick, not per frame.
 *
 * The handler only counts, it never changes or drops a packet. If the server's internals cannot be found (another
 * server software), nothing is measured.
 */
final class InputRhythm implements Listener {
    static final String HANDLER = "mcdatalink_rhythm";
    static final int BURST_MS = 500;
    static final int DOUBLE_MS = 25;
    /** Upper bounds of the interval histogram in ms; the last bucket is everything up to BURST_MS. */
    static final int[] HIST = {30, 50, 70, 90, 120, 160, 250, BURST_MS};
    static final int STALL_TICK_MS = 250;
    static final int STALL_MOVE_MS = 1500;
    /** A swing within this time after a right click packet belongs to it (both are sent in the same tick). */
    static final int USE_MS = 25;
    /** While mining, the client swings every tick; a longer pause between swings means mining has ended. */
    static final int DIG_PAUSE_MS = 150;

    private final MCDataLink plugin;
    private final Map<UUID, Stats> stats = new ConcurrentHashMap<>();
    private final Map<Class<?>, Kind> kinds = new ConcurrentHashMap<>();
    private volatile Field actionField;
    private volatile boolean actionFieldSearched;
    private boolean warned;

    enum Kind { SWING, USE, DIG, MOVE, TICK_END, OTHER }

    InputRhythm(MCDataLink plugin) {
        this.plugin = plugin;
    }

    void start() {
        plugin.getServer().getPluginManager().registerEvents(this, plugin);
        for (Player player : plugin.getServer().getOnlinePlayers()) inject(player);  // after a reload
    }

    void stop() {
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            Channel channel = channel(player);
            if (channel != null) channel.eventLoop().execute(() -> remove(channel.pipeline()));
        }
        stats.clear();
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        inject(event.getPlayer());
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        stats.remove(event.getPlayer().getUniqueId());
    }

    private void inject(Player player) {
        Channel channel = channel(player);
        if (channel == null) {
            if (!warned) plugin.getLogger().info("Click and rhythm analysis not available on this server software.");
            warned = true;
            return;
        }
        Stats s = new Stats();
        stats.put(player.getUniqueId(), s);
        channel.eventLoop().execute(() -> {
            ChannelPipeline pipeline = channel.pipeline();
            remove(pipeline);
            if (pipeline.get("packet_handler") == null) return;
            try {
                pipeline.addBefore("packet_handler", HANDLER, new Handler(s));
            } catch (RuntimeException e) {
                plugin.getLogger().fine("Rhythm handler not added: " + e.getMessage());
            }
        });
    }

    private static void remove(ChannelPipeline pipeline) {
        try {
            if (pipeline.get(HANDLER) != null) pipeline.remove(HANDLER);
        } catch (RuntimeException ignored) {
            // the channel is closing
        }
    }

    /** The JSON fields of one player for the minute report, then the counters start over. Empty without data. */
    String report(UUID uuid) {
        Stats s = stats.get(uuid);
        return s == null ? "" : s.drain();
    }

    /** The player's Netty channel: handle -> connection (game listener) -> network manager -> channel, by type. */
    static Channel channel(Player player) {
        try {
            Object handle = player.getClass().getMethod("getHandle").invoke(player);
            return find(handle, 0);
        } catch (ReflectiveOperationException | RuntimeException | LinkageError e) {
            return null;
        }
    }

    private static Channel find(Object target, int depth) throws IllegalAccessException {
        for (Class<?> type = target.getClass(); type != null && type != Object.class; type = type.getSuperclass()) {
            for (Field field : type.getDeclaredFields()) {
                if (Modifier.isStatic(field.getModifiers())) continue;
                if (Channel.class.isAssignableFrom(field.getType())) {
                    field.setAccessible(true);
                    Object value = field.get(target);
                    if (value instanceof Channel) return (Channel) value;
                }
            }
        }
        if (depth >= 2) return null;
        for (Class<?> type = target.getClass(); type != null && type != Object.class; type = type.getSuperclass()) {
            for (Field field : type.getDeclaredFields()) {
                if (Modifier.isStatic(field.getModifiers()) || field.getType().isPrimitive()) continue;
                String name = field.getType().getName();
                boolean listener = name.contains("PlayerConnection") || name.contains("PacketListener")
                        || name.endsWith(".Connection") || name.contains("NetworkManager");
                if (!listener || !name.startsWith("net.minecraft")) continue;
                field.setAccessible(true);
                Object value = field.get(target);
                if (value == null) continue;
                Channel channel = find(value, depth + 1);
                if (channel != null) return channel;
            }
        }
        return null;
    }

    Kind kind(Class<?> type) {
        Kind kind = kinds.get(type);
        if (kind != null) return kind;
        kind = classify(type.getName());
        kinds.put(type, kind);
        return kind;
    }

    /** Mojang names (Paper 1.20.5+) and Spigot names of the serverbound packets. */
    static Kind classify(String name) {
        if (name.contains("SwingPacket") || name.contains("PacketPlayInArmAnimation")) return Kind.SWING;
        if (name.contains("UseItem") || name.contains("PacketPlayInBlockPlace")) return Kind.USE;
        if (name.contains("PlayerActionPacket") || name.contains("PacketPlayInBlockDig")) return Kind.DIG;
        if (name.contains("ClientTickEnd")) return Kind.TICK_END;
        if (name.contains("MovePlayerPacket") || name.contains("PacketPlayInFlying")) return Kind.MOVE;
        return Kind.OTHER;
    }

    /** START (mining begins), STOP or ABORT (it ends), or null for other actions or if unknown. */
    String digAction(Object packet) {
        if (!actionFieldSearched) {
            for (Field field : packet.getClass().getDeclaredFields()) {
                if (field.getType().isEnum() && !Modifier.isStatic(field.getModifiers())) {
                    field.setAccessible(true);
                    actionField = field;
                    break;
                }
            }
            actionFieldSearched = true;
        }
        Field field = actionField;
        if (field == null) return null;
        try {
            Object value = field.get(packet);
            if (!(value instanceof Enum)) return null;
            String name = ((Enum<?>) value).name();
            if (name.startsWith("START")) return "START";
            if (name.startsWith("STOP") || name.startsWith("ABORT")) return "STOP";
            return null;
        } catch (IllegalAccessException | RuntimeException e) {
            return null;
        }
    }

    private final class Handler extends ChannelDuplexHandler {
        private final Stats stats;

        Handler(Stats stats) {
            this.stats = stats;
        }

        @Override
        public void channelRead(ChannelHandlerContext ctx, Object msg) throws Exception {
            try {
                Kind kind = kind(msg.getClass());
                stats.packet(System.nanoTime() / 1_000_000L, kind, kind == Kind.DIG ? digAction(msg) : null);
            } catch (RuntimeException ignored) {
                // never break the connection because of a measurement
            }
            super.channelRead(ctx, msg);
        }
    }

    /** The counters of one player for one minute. Written by the Netty thread, read by the main thread. */
    static final class Stats {
        private long lastPacket = -1;
        private long lastClick = -1;
        private long lastUse = -1;
        private boolean digging;
        private long lastDig = -1;
        private boolean tickClient;
        private final long[] recent = new long[64];  // click times for the clicks per second
        private int recentCount;
        private int recentStart;
        // per minute
        private int clicks;
        private int cpsMax;
        private int n;
        private long sum;
        private long squares;
        private int min = Integer.MAX_VALUE;
        private int doubles;
        private final int[] hist = new int[HIST.length];
        private int gapMax;
        private int stalls;
        private int ticks;

        synchronized void packet(long now, Kind kind, String digAction) {
            if (lastPacket >= 0) {
                int gap = (int) Math.min(Integer.MAX_VALUE, now - lastPacket);
                if (gap > gapMax) gapMax = gap;
                if (gap >= (tickClient ? STALL_TICK_MS : STALL_MOVE_MS)) stalls++;
            }
            lastPacket = now;
            switch (kind) {
                case TICK_END:
                    tickClient = true;
                    ticks++;
                    break;
                case USE:
                    lastUse = now;
                    break;
                case DIG:
                    if ("START".equals(digAction)) {
                        digging = true;
                        lastDig = now;
                    } else if ("STOP".equals(digAction)) {
                        digging = false;
                    }
                    break;
                case SWING:
                    // mining swings every tick; a pause ends it (an instantly broken block sends no STOP)
                    if (digging && now - lastDig > DIG_PAUSE_MS) digging = false;
                    if (digging) lastDig = now;
                    else if (lastUse < 0 || now - lastUse > USE_MS) click(now);
                    break;
                default:
                    break;
            }
        }

        private void click(long now) {
            clicks++;
            if (lastClick >= 0) {
                long interval = now - lastClick;
                if (interval < BURST_MS) {
                    int ms = (int) interval;
                    n++;
                    sum += ms;
                    squares += (long) ms * ms;
                    if (ms < min) min = ms;
                    if (ms < DOUBLE_MS) doubles++;
                    for (int i = 0; i < HIST.length; i++) {
                        if (ms < HIST[i]) {
                            hist[i]++;
                            break;
                        }
                    }
                }
            }
            lastClick = now;
            // clicks within the last second
            while (recentCount > 0 && now - recent[recentStart] >= 1000) {
                recentStart = (recentStart + 1) % recent.length;
                recentCount--;
            }
            if (recentCount == recent.length) {
                recentStart = (recentStart + 1) % recent.length;
                recentCount--;
            }
            recent[(recentStart + recentCount) % recent.length] = now;
            recentCount++;
            if (recentCount > cpsMax) cpsMax = recentCount;
        }

        synchronized String drain() {
            if (lastPacket < 0) return "";
            StringBuilder json = new StringBuilder();
            json.append(",\"rhythm\":\"").append(tickClient ? "tick" : "move").append('"');
            json.append(",\"gap_max\":").append(gapMax).append(",\"stalls\":").append(stalls);
            if (tickClient) json.append(",\"ticks\":").append(ticks);
            json.append(",\"clicks\":").append(clicks);
            if (clicks > 0) {
                json.append(",\"cps_max\":").append(cpsMax).append(",\"click_n\":").append(n)
                        .append(",\"click_sum\":").append(sum).append(",\"click_sq\":").append(squares)
                        .append(",\"doubles\":").append(doubles);
                if (n > 0) json.append(",\"click_min\":").append(min);
                json.append(",\"click_hist\":[");
                for (int i = 0; i < hist.length; i++) json.append(i == 0 ? "" : ",").append(hist[i]);
                json.append(']');
            }
            clicks = cpsMax = n = doubles = gapMax = stalls = ticks = 0;
            sum = squares = 0;
            min = Integer.MAX_VALUE;
            java.util.Arrays.fill(hist, 0);
            return json.toString();
        }
    }
}
