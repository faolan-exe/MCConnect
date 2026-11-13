package org.tobias.mcconnect.discoveries;

import org.bukkit.Location;
import org.bukkit.World;
import org.tobias.mcdatalink.api.AddonConfig;

import java.util.Locale;
import java.util.regex.Pattern;

/** One place from the settings: a box between two corners (in any order) or a circle, with its advancement texts. */
final class Area {
    static final Pattern NAME = Pattern.compile("[A-Za-z0-9_-]{1,32}");

    /** lower case: the id of the place (advancement key, MCConnect) */
    final String id;
    final String name, title, description, icon;
    final boolean enabled;
    /** empty: the main world */
    final String world;
    final boolean circle;
    final int minX, minY, minZ, maxX, maxY, maxZ;
    final int centerX, centerY, centerZ;
    final int radius;
    /** circle: blocks above the center that still count, 0 = no limit */
    final int height;

    Area(AddonConfig row) {
        name = row.getString("name", "ort").trim();
        id = name.toLowerCase(Locale.ROOT);
        title = row.getString("title", name).trim().isEmpty() ? name : row.getString("title", name).trim();
        description = row.getString("description", "").trim();
        String material = row.getString("icon", "").trim().toLowerCase(Locale.ROOT);
        icon = material.isEmpty() ? "filled_map" : material;
        enabled = row.getBoolean("enabled", true);
        world = row.getString("world", "").trim();
        circle = "circle".equals(row.getString("shape", "rectangle"));
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
    }

    boolean contains(Location location, World main) {
        World candidate = location.getWorld();
        if (candidate == null) return false;
        if (!(world.isEmpty() ? candidate.equals(main) : candidate.getName().equalsIgnoreCase(world))) return false;
        if (!circle) return inBox(location.getBlockX(), location.getBlockY(), location.getBlockZ());
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
        if (!circle) return "Rechteck " + (maxX - minX + 1) + " × " + (maxZ - minZ + 1) + ", Y " + minY + " bis " + maxY;
        return "Kreis mit Radius " + radius + " um " + centerX + " " + centerY + " " + centerZ + (height > 0 ? ", " + height + " Blöcke hoch" : "");
    }
}
