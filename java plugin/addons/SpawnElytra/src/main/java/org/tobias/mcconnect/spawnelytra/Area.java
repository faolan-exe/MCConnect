package org.tobias.mcconnect.spawnelytra;

import org.bukkit.Location;
import org.bukkit.World;
import org.tobias.mcdatalink.api.AddonConfig;

/** One area from the settings, with its own boost: a box between two corners (in any order) or a circle. */
final class Area {
    final String name;
    /** empty: the main world */
    final String world;
    final boolean circle, aroundSpawn;
    final int minX, minY, minZ, maxX, maxY, maxZ;
    final int centerX, centerY, centerZ;
    final int radius;
    /** circle: blocks above the center that still count, 0 = no limit */
    final int height;
    final double boost;

    private Area(AddonConfig row) {
        name = row.getString("name", "bereich");
        world = row.getString("world", "").trim();
        String shape = row.getString("shape", "rectangle");
        aroundSpawn = "spawn".equals(shape);
        circle = aroundSpawn || "circle".equals(shape);
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
        int[] center = row.getPosition("center");
        if (center == null) center = new int[]{0, 0, 0};
        centerX = center[0];
        centerY = center[1];
        centerZ = center[2];
        radius = Math.max(1, row.getInt("radius", 20));
        height = row.getInt("height", 0);
        boost = row.getDouble("boost", 1.0);
    }

    /** The area of a settings row, or null if it is switched off. */
    static Area of(AddonConfig row) {
        return row.getBoolean("enabled", true) ? new Area(row) : null;
    }

    boolean contains(Location location, World main) {
        World candidate = location.getWorld();
        if (candidate == null) return false;
        if (!(world.isEmpty() ? candidate.equals(main) : candidate.getName().equalsIgnoreCase(world))) return false;
        if (!circle) return inBox(location.getBlockX(), location.getBlockY(), location.getBlockZ());
        if (aroundSpawn) {
            Location spawn = candidate.getSpawnLocation();
            return inCircle(location.getX() - spawn.getX(), location.getZ() - spawn.getZ(), location.getBlockY(), spawn.getBlockY());
        }
        return inCircle(location.getX() - (centerX + 0.5), location.getZ() - (centerZ + 0.5), location.getBlockY(), centerY);
    }

    /** Both corners included. */
    boolean inBox(int x, int y, int z) {
        return x >= minX && x <= maxX && y >= minY && y <= maxY && z >= minZ && z <= maxZ;
    }

    /** dx, dz: horizontal distance to the center; in the height from its level up to height blocks above, both included. */
    boolean inCircle(double dx, double dz, int y, int levelOfCenter) {
        if (height > 0 && (y < levelOfCenter || y > levelOfCenter + height)) return false;
        return dx * dx + dz * dz <= (double) radius * radius;
    }

    String size() {
        if (!circle) return "Rechteck " + (maxX - minX + 1) + " × " + (maxZ - minZ + 1) + ", Y " + minY + " bis " + maxY + ", Schub " + boost;
        return "Kreis mit Radius " + radius + " um " + (aroundSpawn ? "den Weltspawn" : centerX + " " + centerY + " " + centerZ)
                + (height > 0 ? ", " + height + " Blöcke hoch" : "") + ", Schub " + boost;
    }
}
