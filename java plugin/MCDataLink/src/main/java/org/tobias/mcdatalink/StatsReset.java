package org.tobias.mcdatalink;

import org.bukkit.Material;
import org.bukkit.Statistic;
import org.bukkit.entity.EntityType;
import org.bukkit.entity.Player;

/** Sets the statistics of an online player to 0 (the game keeps them in memory and writes them back to the file). */
final class StatsReset {
    private StatsReset() {
    }

    /** Main thread. */
    static void clear(Player player) {
        for (Statistic statistic : Statistic.values()) {
            switch (statistic.getType()) {
                case UNTYPED:
                    if (player.getStatistic(statistic) > 0) player.setStatistic(statistic, 0);
                    break;
                case ENTITY:
                    for (EntityType entity : EntityType.values()) clear(player, statistic, entity);
                    break;
                default:  // BLOCK and ITEM: what doesn't fit is refused
                    for (Material material : Material.values()) {
                        if (!material.isLegacy()) clear(player, statistic, material);
                    }
            }
        }
    }

    private static void clear(Player player, Statistic statistic, Material material) {
        try {
            if (player.getStatistic(statistic, material) > 0) player.setStatistic(statistic, material, 0);
        } catch (IllegalArgumentException e) {
            // e.g. a block that can't be used as an item
        }
    }

    private static void clear(Player player, Statistic statistic, EntityType entity) {
        try {
            if (player.getStatistic(statistic, entity) > 0) player.setStatistic(statistic, entity, 0);
        } catch (IllegalArgumentException e) {
            // e.g. an entity without statistics
        }
    }
}
