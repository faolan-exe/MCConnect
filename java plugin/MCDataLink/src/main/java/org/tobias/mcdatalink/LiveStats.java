package org.tobias.mcdatalink;

import org.bukkit.Material;
import org.bukkit.Statistic;
import org.bukkit.entity.Player;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Sends the current stats of online players every minute, read with the Bukkit API. Minecraft only
 * writes the stats files on autosave (default every 5 minutes) and on quit, so without this new
 * achievements and rankings would lag behind.
 *
 * Only the values MCConnect uses for rankings and achievements are sent, under their vanilla names
 * (the same names as in the stats file, so both sources update the same values). Bukkit names some
 * statistics differently, e.g. PLAY_ONE_MINUTE is "minecraft:play_time" since 1.17.
 */
final class LiveStats {
    /** Bukkit statistic -> vanilla custom stat. Missing enum constants (older versions) are skipped. */
    private static final String[][] CUSTOM = {
            {"MOB_KILLS", "mob_kills"}, {"DEATHS", "deaths"}, {"JUMP", "jump"},
            {"DAMAGE_DEALT", "damage_dealt"}, {"DAMAGE_TAKEN", "damage_taken"}, {"PLAYER_KILLS", "player_kills"},
            {"FISH_CAUGHT", "fish_caught"}, {"TRADED_WITH_VILLAGER", "traded_with_villager"},
            {"ANIMALS_BRED", "animals_bred"},
            {"WALK_ONE_CM", "walk_one_cm"}, {"SPRINT_ONE_CM", "sprint_one_cm"}, {"CROUCH_ONE_CM", "crouch_one_cm"},
            {"SWIM_ONE_CM", "swim_one_cm"}, {"WALK_ON_WATER_ONE_CM", "walk_on_water_one_cm"},
            {"WALK_UNDER_WATER_ONE_CM", "walk_under_water_one_cm"}, {"CLIMB_ONE_CM", "climb_one_cm"},
            {"FLY_ONE_CM", "fly_one_cm"}, {"AVIATE_ONE_CM", "aviate_one_cm"}, {"BOAT_ONE_CM", "boat_one_cm"},
            {"HORSE_ONE_CM", "horse_one_cm"}, {"MINECART_ONE_CM", "minecart_one_cm"}, {"PIG_ONE_CM", "pig_one_cm"},
            {"STRIDER_ONE_CM", "strider_one_cm"},
    };
    private static final long INTERVAL_TICKS = 20L * 60;

    private final MCDataLink plugin;
    private final Map<Statistic, String> custom = new LinkedHashMap<>();
    private final List<Material> blocks = new ArrayList<>();

    LiveStats(MCDataLink plugin) {
        this.plugin = plugin;
        for (String[] entry : CUSTOM) {
            try {
                custom.put(Statistic.valueOf(entry[0]), "minecraft:" + entry[1]);
            } catch (IllegalArgumentException ignored) {
                // statistic does not exist in this version
            }
        }
        custom.put(Statistic.PLAY_ONE_MINUTE, playTimeName(plugin.getServer().getBukkitVersion()));
        for (Material material : Material.values()) {
            if (material.isBlock() && !material.isLegacy()) blocks.add(material);
        }
    }

    /** "minecraft:play_time" since 1.17, "minecraft:play_one_minute" before. */
    static String playTimeName(String bukkitVersion) {
        String[] parts = bukkitVersion.split("[.-]");
        try {
            return parts.length > 1 && Integer.parseInt(parts[1]) >= 17 ? "minecraft:play_time" : "minecraft:play_one_minute";
        } catch (NumberFormatException e) {
            return "minecraft:play_time";
        }
    }

    void start() {
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::sendAll, INTERVAL_TICKS, INTERVAL_TICKS);
    }

    /** Main thread (the statistics API is not thread safe). */
    private void sendAll() {
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            plugin.sendLiveStats(player.getUniqueId(), json(player));
        }
    }

    private String json(Player player) {
        StringBuilder out = new StringBuilder("{\"stats\":{\"minecraft:custom\":{");
        boolean first = true;
        for (Map.Entry<Statistic, String> entry : custom.entrySet()) {
            int value = value(player, entry.getKey(), null);
            if (value <= 0) continue;
            if (!first) out.append(',');
            out.append('"').append(entry.getValue()).append("\":").append(value);
            first = false;
        }
        out.append("},\"minecraft:mined\":");
        appendBlocks(out, player, Statistic.MINE_BLOCK);
        out.append(",\"minecraft:used\":");
        appendBlocks(out, player, Statistic.USE_ITEM);  // placed blocks
        return out.append("}}").toString();
    }

    private void appendBlocks(StringBuilder out, Player player, Statistic statistic) {
        out.append('{');
        boolean first = true;
        for (Material block : blocks) {
            int value = value(player, statistic, block);
            if (value <= 0) continue;
            if (!first) out.append(',');
            out.append('"').append(block.getKey()).append("\":").append(value);
            first = false;
        }
        out.append('}');
    }

    private static int value(Player player, Statistic statistic, Material material) {
        try {
            return material == null ? player.getStatistic(statistic) : player.getStatistic(statistic, material);
        } catch (IllegalArgumentException e) {
            return 0;  // e.g. a block that can't be used as an item
        }
    }
}
