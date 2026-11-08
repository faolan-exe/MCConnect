package org.tobias.mcconnect.spawnelytra;

import org.bukkit.Location;
import org.bukkit.World;
import org.tobias.mcdatalink.api.AddonConfig;

/** One rectangle from the settings: between two corners (in any order), with its own boost. */
final class Area {
    final String name;
    /** empty: the main world */
    final String world;
    final int minX, minY, minZ, maxX, maxY, maxZ;
    final double boost;

    private Area(AddonConfig row) {
        name = row.getString("name", "bereich");
        world = row.getString("world", "").trim();
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
        boost = row.getDouble("boost", 1.0);
    }

    /** The area of a settings row, or null if it is switched off. */
    static Area of(AddonConfig row) {
        return row.getBoolean("enabled", true) ? new Area(row) : null;
    }

    boolean contains(Location location, World main, int height) {
        World candidate = location.getWorld();
        if (candidate == null) return false;
        if (!(world.isEmpty() ? candidate.equals(main) : candidate.getName().equalsIgnoreCase(world))) return false;
        return covers(location.getBlockX(), location.getBlockY(), location.getBlockZ(), height);
    }

    /** Both corners included; in the height from the lower corner up to height blocks above the higher one (0 = no limit). */
    boolean covers(int x, int y, int z, int height) {
        return x >= minX && x <= maxX && z >= minZ && z <= maxZ && (height <= 0 || (y >= minY && y <= maxY + height));
    }

    String size() {
        return (maxX - minX + 1) + " × " + (maxZ - minZ + 1) + " Blöcke, Schub " + boost;
    }
}
