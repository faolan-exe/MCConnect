package org.tobias.mcconnect.spawnelytra;

import org.bukkit.ChatColor;
import org.bukkit.Color;
import org.bukkit.Location;
import org.bukkit.Particle;
import org.bukkit.Server;
import org.bukkit.World;
import org.bukkit.entity.ArmorStand;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;

import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * /spawnelytra show: the outlines of the areas as particles and their names as floating labels, only for the player who
 * asked and for a minute. Particles only near the player; labels are invisible marker armor stands that are never saved
 * and, on servers that can (1.19.3+), only shown to that player.
 */
final class Outline {
    static final long DURATION_MS = 60_000L;
    private static final double RANGE = 48;
    private static final double LABEL_RANGE = 96;
    private static final int MAX_STEPS = 4000;
    private static final String TAG = "spawnelytra-label";
    /** renamed from REDSTONE in 1.20.5 */
    private static final Particle DUST = particle("DUST", "REDSTONE");
    private static final Particle.DustOptions ON = new Particle.DustOptions(Color.fromRGB(0x34c46a), 1.3f);
    private static final Particle.DustOptions OFF = new Particle.DustOptions(Color.fromRGB(0x9a9a9a), 1.0f);
    private static final Method VISIBLE_BY_DEFAULT = method(Entity.class, "setVisibleByDefault", boolean.class);
    private static final Method SHOW_ENTITY = method(Player.class, "showEntity", Plugin.class, Entity.class);

    private final Plugin plugin;
    private final Map<UUID, Long> until = new HashMap<>();
    /** per viewer: label key (text and place) → armor stand */
    private final Map<UUID, Map<String, ArmorStand>> labels = new HashMap<>();

    Outline(Plugin plugin) {
        this.plugin = plugin;
    }

    boolean showing(Player player) {
        return until.containsKey(player.getUniqueId());
    }

    /** Switches it on (again for a full minute) or off. */
    void set(Player player, boolean on) {
        if (on) until.put(player.getUniqueId(), System.currentTimeMillis() + DURATION_MS);
        else stop(player.getUniqueId());
    }

    /** After an edit: keep showing for another minute, if it is on. */
    void extend(Player player) {
        if (showing(player)) set(player, true);
    }

    void stop(UUID uuid) {
        until.remove(uuid);
        Map<String, ArmorStand> mine = labels.remove(uuid);
        if (mine != null) for (ArmorStand stand : mine.values()) stand.remove();
    }

    void stopAll() {
        for (UUID uuid : new HashSet<>(until.keySet())) stop(uuid);
    }

    /** Labels left over from a crash (they are not saved, but a server stop without onDisable is possible). */
    static void removeLeftovers(Server server) {
        for (World world : server.getWorlds()) {
            for (ArmorStand stand : world.getEntitiesByClass(ArmorStand.class)) {
                if (stand.getScoreboardTags().contains(TAG)) stand.remove();
            }
        }
    }

    /** Every few ticks: the particles, and with labels the floating names, for everyone who asked. */
    void draw(List<Area> areas, World main, boolean withLabels) {
        long now = System.currentTimeMillis();
        for (Iterator<Map.Entry<UUID, Long>> it = until.entrySet().iterator(); it.hasNext(); ) {
            Map.Entry<UUID, Long> entry = it.next();
            Player player = plugin.getServer().getPlayer(entry.getKey());
            if (player == null || entry.getValue() < now) {
                it.remove();
                Map<String, ArmorStand> mine = labels.remove(entry.getKey());
                if (mine != null) for (ArmorStand stand : mine.values()) stand.remove();
                if (player != null) player.sendMessage(ChatColor.GRAY + "SpawnElytra: Bereiche werden nicht mehr angezeigt.");
                continue;
            }
            for (Area area : areas) {
                if (area.inWorld(player.getWorld(), main)) drawArea(player, area);
            }
            if (withLabels) updateLabels(player, areas, main);
        }
    }

    // ------------------------------------------------------------------ particles

    private void drawArea(Player player, Area area) {
        Particle.DustOptions color = area.enabled ? ON : OFF;
        if (!area.circle) {
            double x0 = area.minX, y0 = area.minY, z0 = area.minZ;
            double x1 = area.maxX + 1, y1 = area.maxY + 1, z1 = area.maxZ + 1;
            for (double y : new double[]{y0, y1}) {
                line(player, x0, y, z0, x1, y, z0, color);
                line(player, x0, y, z1, x1, y, z1, color);
                line(player, x0, y, z0, x0, y, z1, color);
                line(player, x1, y, z0, x1, y, z1, color);
            }
            for (double[] corner : new double[][]{{x0, z0}, {x1, z0}, {x0, z1}, {x1, z1}}) {
                line(player, corner[0], y0, corner[1], corner[0], y1, corner[1], color);
            }
            return;
        }
        double cx, cz;
        int level;
        if (area.aroundSpawn) {
            Location spawn = player.getWorld().getSpawnLocation();
            cx = spawn.getX();
            cz = spawn.getZ();
            level = spawn.getBlockY();
        } else {
            cx = area.centerX + 0.5;
            cz = area.centerZ + 0.5;
            level = area.centerY;
        }
        if (area.height > 0) {
            double top = level + area.height + 1;
            ring(player, cx, level, cz, area.radius, color);
            ring(player, cx, top, cz, area.radius, color);
            for (double[] d : new double[][]{{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) {
                double x = cx + d[0] * area.radius, z = cz + d[1] * area.radius;
                line(player, x, level, z, x, top, z, color);
            }
        } else {
            ring(player, cx, player.getLocation().getBlockY() + 0.1, cz, area.radius, color);  // no height limit: at the player's feet
        }
    }

    private static void line(Player player, double x0, double y0, double z0, double x1, double y1, double z1, Particle.DustOptions color) {
        double length = Math.sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) + (z1 - z0) * (z1 - z0));
        int steps = (int) Math.min(MAX_STEPS, Math.max(1, Math.ceil(length)));
        for (int i = 0; i <= steps; i++) {
            double t = (double) i / steps;
            dot(player, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t, color);
        }
    }

    private static void ring(Player player, double cx, double y, double cz, int radius, Particle.DustOptions color) {
        int steps = (int) Math.min(MAX_STEPS, Math.max(16, Math.ceil(2 * Math.PI * radius)));
        for (int i = 0; i < steps; i++) {
            double angle = 2 * Math.PI * i / steps;
            dot(player, cx + Math.cos(angle) * radius, y, cz + Math.sin(angle) * radius, color);
        }
    }

    private static void dot(Player player, double x, double y, double z, Particle.DustOptions color) {
        Location eye = player.getEyeLocation();
        double dx = x - eye.getX(), dy = y - eye.getY(), dz = z - eye.getZ();
        if (dx * dx + dy * dy + dz * dz > RANGE * RANGE) return;
        if (DUST != null) player.spawnParticle(DUST, x, y, z, 1, 0, 0, 0, 0, color);
        else player.spawnParticle(Particle.FLAME, x, y, z, 1, 0, 0, 0, 0);
    }

    // ------------------------------------------------------------------ labels

    private void updateLabels(Player player, List<Area> areas, World main) {
        Map<String, ArmorStand> mine = labels.computeIfAbsent(player.getUniqueId(), k -> new HashMap<>());
        Map<String, Location> wanted = new HashMap<>();
        for (Area area : areas) {
            if (!area.inWorld(player.getWorld(), main)) continue;
            Location at = labelPoint(area, player.getWorld());
            if (at.distanceSquared(player.getLocation()) > LABEL_RANGE * LABEL_RANGE) continue;
            wanted.put(text(area) + "@" + at.getBlockX() + "," + at.getBlockY() + "," + at.getBlockZ(), at);
        }
        for (Iterator<Map.Entry<String, ArmorStand>> it = mine.entrySet().iterator(); it.hasNext(); ) {
            Map.Entry<String, ArmorStand> entry = it.next();
            if (!wanted.containsKey(entry.getKey()) || !entry.getValue().isValid()) {
                entry.getValue().remove();
                it.remove();
            }
        }
        Set<String> missing = new HashSet<>(wanted.keySet());
        missing.removeAll(mine.keySet());
        for (String key : missing) {
            Location at = wanted.get(key);
            if (!at.getWorld().isChunkLoaded(at.getBlockX() >> 4, at.getBlockZ() >> 4)) continue;
            mine.put(key, spawnLabel(player, at, key.substring(0, key.lastIndexOf('@'))));
        }
    }

    private static Location labelPoint(Area area, World world) {
        if (!area.circle) {
            return new Location(world, (area.minX + area.maxX + 1) / 2.0, area.maxY + 1.2, (area.minZ + area.maxZ + 1) / 2.0);
        }
        if (area.aroundSpawn) return world.getSpawnLocation().clone().add(0, 2.2, 0);
        return new Location(world, area.centerX + 0.5, area.centerY + 2.2, area.centerZ + 0.5);
    }

    private static String text(Area area) {
        return area.enabled
                ? ChatColor.GOLD + area.name + ChatColor.GRAY + " · Schub " + area.boost
                : ChatColor.GRAY + area.name + " (aus)";
    }

    private ArmorStand spawnLabel(Player viewer, Location at, String text) {
        ArmorStand stand = at.getWorld().spawn(at, ArmorStand.class);
        stand.setVisible(false);
        stand.setMarker(true);
        stand.setGravity(false);
        stand.setSmall(true);
        stand.setInvulnerable(true);
        stand.setSilent(true);
        stand.setPersistent(false);
        stand.addScoreboardTag(TAG);
        stand.setCustomName(text);
        stand.setCustomNameVisible(true);
        if (VISIBLE_BY_DEFAULT != null && SHOW_ENTITY != null) {
            try {
                VISIBLE_BY_DEFAULT.invoke(stand, false);
                SHOW_ENTITY.invoke(viewer, plugin, stand);
            } catch (ReflectiveOperationException | RuntimeException ignored) {
                // older server: everyone nearby sees the label for this minute
            }
        }
        return stand;
    }

    private static Particle particle(String... names) {
        for (String name : names) {
            try {
                return Particle.valueOf(name);
            } catch (IllegalArgumentException ignored) {
                // try the next name
            }
        }
        return null;
    }

    private static Method method(Class<?> type, String name, Class<?>... parameters) {
        try {
            return type.getMethod(name, parameters);
        } catch (NoSuchMethodException e) {
            return null;
        }
    }
}
