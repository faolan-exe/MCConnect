package org.tobias.mcdatalink;

import org.bukkit.World;

import java.lang.management.ManagementFactory;
import java.util.ArrayDeque;
import java.util.Locale;

/**
 * Reports the server health to MCConnect once a minute: TPS, memory, players, loaded chunks,
 * entities, uptime and versions. The TPS is measured with a task that runs every 20 ticks
 * (Bukkit has no TPS API; Paper's getTPS() is not available on Spigot).
 */
final class HealthReporter {
    private static final long MEASURE_INTERVAL_TICKS = 20L;
    private static final long REPORT_INTERVAL_TICKS = 20L * 60;
    /** TPS average over the last minute. */
    private static final int SAMPLES = 60;

    private final MCDataLink plugin;
    private final ArrayDeque<Long> intervals = new ArrayDeque<>();
    private long lastTick;

    HealthReporter(MCDataLink plugin) {
        this.plugin = plugin;
    }

    void start() {
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::measure, MEASURE_INTERVAL_TICKS, MEASURE_INTERVAL_TICKS);
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::report, REPORT_INTERVAL_TICKS, REPORT_INTERVAL_TICKS);
    }

    /** Main thread, every 20 ticks: the wall time of 20 ticks is one second on a healthy server. */
    private void measure() {
        long now = System.nanoTime();
        if (lastTick != 0) {
            intervals.addLast(now - lastTick);
            if (intervals.size() > SAMPLES) intervals.removeFirst();
        }
        lastTick = now;
    }

    private double tps() {
        if (intervals.isEmpty()) return 20.0;
        long total = 0;
        for (long interval : intervals) total += interval;
        double secondsPerInterval = total / (double) intervals.size() / 1_000_000_000.0;
        return Math.min(20.0, MEASURE_INTERVAL_TICKS / secondsPerInterval);
    }

    /** Main thread: collect the values (world access) and send them from the worker thread. */
    void report() {
        int chunks = 0;
        int entities = 0;
        for (World world : plugin.getServer().getWorlds()) {
            chunks += world.getLoadedChunks().length;
            entities += world.getEntities().size();
        }
        Runtime runtime = Runtime.getRuntime();
        long usedMb = (runtime.totalMemory() - runtime.freeMemory()) / (1024 * 1024);
        long maxMb = runtime.maxMemory() / (1024 * 1024);
        long uptimeSeconds = ManagementFactory.getRuntimeMXBean().getUptime() / 1000;
        String json = String.format(Locale.ROOT,
                "{\"tps\":%.2f,\"mem_used_mb\":%d,\"mem_max_mb\":%d,\"players\":%d,\"chunks\":%d,\"entities\":%d,"
                        + "\"uptime_s\":%d,\"mc_version\":\"%s\",\"plugin_version\":\"%s\"}",
                tps(), usedMb, maxMb, plugin.getServer().getOnlinePlayers().size(), chunks, entities, uptimeSeconds,
                clean(plugin.getServer().getBukkitVersion()), clean(plugin.getDescription().getVersion()));
        plugin.sendHealth(json);
    }

    /** Versions are plain text; drop anything that would break the JSON string. */
    private static String clean(String text) {
        return text == null ? "" : text.replaceAll("[\"\\\\\\p{Cntrl}]", "");
    }
}
