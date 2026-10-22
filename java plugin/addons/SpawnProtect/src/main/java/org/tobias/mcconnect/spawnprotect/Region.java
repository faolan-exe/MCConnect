package org.tobias.mcconnect.spawnprotect;

import org.bukkit.Location;
import org.bukkit.World;
import org.tobias.mcdatalink.api.AddonConfig;

import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** One protected area from the settings: a box between two corners (in any order) or a circle around the world spawn. */
final class Region {
    static final List<String> FLAGS = java.util.Arrays.asList("build", "interact", "pvp", "damage", "hunger", "mobs", "explosions",
            "fire", "liquids", "pistons", "trample", "animals", "mob_grief");

    final String name;
    /** empty: the main world */
    final String world;
    final boolean aroundSpawn;
    final int minX, minY, minZ, maxX, maxY, maxZ;
    final int radius;
    final Set<String> protect;
    final String greeting;

    private Region(AddonConfig row) {
        name = row.getString("name", "bereich");
        world = row.getString("world", "").trim();
        aroundSpawn = "spawn".equals(row.getString("shape", "cuboid"));
        int[] a = row.getPosition("pos1");
        int[] b = row.getPosition("pos2");
        if (a == null) a = new int[]{0, 0, 0};
        if (b == null) b = a;
        minX = Math.min(a[0], b[0]);
        minY = Math.min(a[1], b[1]);
        minZ = Math.min(a[2], b[2]);
        maxX = Math.max(a[0], b[0]);
        maxY = Math.max(a[1], b[1]);
        maxZ = Math.max(a[2], b[2]);
        radius = Math.max(1, row.getInt("radius", 24));
        Set<String> flags = new HashSet<>();
        for (String flag : row.getStringList("protect")) flags.add(flag.toLowerCase(Locale.ROOT));
        protect = Collections.unmodifiableSet(flags);
        greeting = row.getString("greeting", "");
    }

    /** The region of a settings row, or null if it is switched off. */
    static Region of(AddonConfig row) {
        return row.getBoolean("enabled", true) ? new Region(row) : null;
    }

    boolean inWorld(World candidate, World main) {
        return world.isEmpty() ? candidate.equals(main) : candidate.getName().equalsIgnoreCase(world);
    }

    boolean contains(Location location, World main) {
        World candidate = location.getWorld();
        if (candidate == null || !inWorld(candidate, main)) return false;
        if (aroundSpawn) {
            Location spawn = candidate.getSpawnLocation();
            double dx = location.getX() - spawn.getX();
            double dz = location.getZ() - spawn.getZ();
            return dx * dx + dz * dz <= (double) radius * radius;
        }
        int x = location.getBlockX(), y = location.getBlockY(), z = location.getBlockZ();
        return x >= minX && x <= maxX && y >= minY && y <= maxY && z >= minZ && z <= maxZ;
    }

    String size() {
        if (aroundSpawn) return "Kreis mit Radius " + radius + " um den Weltspawn";
        return (maxX - minX + 1) + " × " + (maxY - minY + 1) + " × " + (maxZ - minZ + 1) + " Blöcke ("
                + minX + " " + minY + " " + minZ + " bis " + maxX + " " + maxY + " " + maxZ + ")";
    }
}
