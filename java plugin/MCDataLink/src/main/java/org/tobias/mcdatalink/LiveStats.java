package org.tobias.mcdatalink;

import org.bukkit.Material;
import org.bukkit.Statistic;
import org.bukkit.entity.EntityType;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.entity.EntityPickupItemEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.inventory.CraftItemEvent;
import org.bukkit.event.player.PlayerDropItemEvent;
import org.bukkit.event.player.PlayerItemBreakEvent;
import org.bukkit.event.player.PlayerItemConsumeEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.inventory.ItemStack;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;

/**
 * Sends the current stats of online players, read with the Bukkit API. Minecraft only writes the stats
 * files on autosave (default every 5 minutes) and on quit, so without this achievements, rankings,
 * competitions and the sidebar would lag behind.
 *
 * Every few seconds ("live-stats-interval") the custom stats (play time, distance, kills, deaths, ...) are
 * sent together with the block/item/mob stats that changed since the last time: events (block break and
 * place, crafting, kills, ...) only remember which statistic changed, the value itself is read from the game
 * when sending. So the values are exactly the vanilla ones and nothing is counted twice. Once a minute the
 * blocks mined and placed are sent completely, which catches everything no event covers.
 *
 * The values have their vanilla names (the same names as in the stats file, so both sources update the same
 * values). Bukkit names some statistics differently, e.g. PLAY_ONE_MINUTE is "minecraft:play_time" since 1.17.
 */
final class LiveStats implements Listener {
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
    private static final long FULL_INTERVAL_TICKS = 20L * 60;

    /** A changed statistic of a player: the statistic with its material or entity type. */
    private static final class Changed {
        final Statistic statistic;
        final Material material;
        final EntityType entity;

        Changed(Statistic statistic, Material material, EntityType entity) {
            this.statistic = statistic;
            this.material = material;
            this.entity = entity;
        }

        @Override
        public boolean equals(Object other) {
            if (!(other instanceof Changed)) return false;
            Changed o = (Changed) other;
            return statistic == o.statistic && material == o.material && entity == o.entity;
        }

        @Override
        public int hashCode() {
            return Objects.hash(statistic, material, entity);
        }
    }

    /** Bukkit statistic -> the stat type in the vanilla stats file. */
    private static final Map<Statistic, String> TYPES = new LinkedHashMap<>();

    static {
        TYPES.put(Statistic.MINE_BLOCK, "minecraft:mined");
        TYPES.put(Statistic.USE_ITEM, "minecraft:used");
        TYPES.put(Statistic.CRAFT_ITEM, "minecraft:crafted");
        TYPES.put(Statistic.BREAK_ITEM, "minecraft:broken");
        TYPES.put(Statistic.PICKUP, "minecraft:picked_up");
        TYPES.put(Statistic.DROP, "minecraft:dropped");
        TYPES.put(Statistic.KILL_ENTITY, "minecraft:killed");
        TYPES.put(Statistic.ENTITY_KILLED_BY, "minecraft:killed_by");
    }

    private final MCDataLink plugin;
    private final long quickTicks;
    private final Map<Statistic, String> custom = new LinkedHashMap<>();
    private final List<Material> blocks = new ArrayList<>();
    /** Statistics changed since the last send, per player. Main thread only. */
    private final Map<UUID, Set<Changed>> changed = new HashMap<>();
    /** Players whose complete stats were sent since they joined: the first update of a player is complete, so
     *  MCConnect never takes a partial one as the first values (baseline). Main thread only. */
    private final Set<UUID> complete = new HashSet<>();

    LiveStats(MCDataLink plugin, int intervalSeconds) {
        this.plugin = plugin;
        this.quickTicks = 20L * Math.max(1, intervalSeconds);
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
        plugin.getServer().getPluginManager().registerEvents(this, plugin);
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::sendChanged, quickTicks, quickTicks);
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::sendAll, FULL_INTERVAL_TICKS, FULL_INTERVAL_TICKS);
    }

    // ------------------------------------------------------------------ what changed (events, main thread)

    private void mark(Player player, Statistic statistic, Material material) {
        if (material == null || material == Material.AIR) return;
        changed.computeIfAbsent(player.getUniqueId(), k -> new LinkedHashSet<>()).add(new Changed(statistic, material, null));
    }

    private void mark(Player player, Statistic statistic, EntityType entity) {
        if (entity == null) return;
        changed.computeIfAbsent(player.getUniqueId(), k -> new LinkedHashSet<>()).add(new Changed(statistic, null, entity));
    }

    private static Material type(ItemStack item) {
        return item == null ? null : item.getType();
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBreak(BlockBreakEvent event) {
        mark(event.getPlayer(), Statistic.MINE_BLOCK, event.getBlock().getType());
        mark(event.getPlayer(), Statistic.USE_ITEM, type(event.getPlayer().getInventory().getItemInMainHand()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPlace(BlockPlaceEvent event) {
        mark(event.getPlayer(), Statistic.USE_ITEM, type(event.getItemInHand()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onCraft(CraftItemEvent event) {
        if (event.getWhoClicked() instanceof Player) {
            mark((Player) event.getWhoClicked(), Statistic.CRAFT_ITEM, type(event.getRecipe().getResult()));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onItemBreak(PlayerItemBreakEvent event) {
        mark(event.getPlayer(), Statistic.BREAK_ITEM, type(event.getBrokenItem()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onConsume(PlayerItemConsumeEvent event) {
        mark(event.getPlayer(), Statistic.USE_ITEM, type(event.getItem()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPickup(EntityPickupItemEvent event) {
        if (event.getEntity() instanceof Player) {
            mark((Player) event.getEntity(), Statistic.PICKUP, type(event.getItem().getItemStack()));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onDrop(PlayerDropItemEvent event) {
        mark(event.getPlayer(), Statistic.DROP, type(event.getItemDrop().getItemStack()));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDeath(EntityDeathEvent event) {
        Player killer = event.getEntity().getKiller();
        if (killer != null) mark(killer, Statistic.KILL_ENTITY, event.getEntityType());
        if (event instanceof PlayerDeathEvent) {
            EntityDamageEvent cause = event.getEntity().getLastDamageCause();
            if (cause instanceof EntityDamageByEntityEvent) {
                mark((Player) event.getEntity(), Statistic.ENTITY_KILLED_BY, ((EntityDamageByEntityEvent) cause).getDamager().getType());
            }
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        changed.remove(event.getPlayer().getUniqueId());  // the stats file is sent after the quit anyway
        complete.remove(event.getPlayer().getUniqueId());
    }

    // ------------------------------------------------------------------ sending (main thread: the statistics API is not thread safe)

    /** Every few seconds: custom stats and what changed. */
    private void sendChanged() {
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            if (complete.add(player.getUniqueId())) {  // first update since the join: everything
                changed.remove(player.getUniqueId());
                plugin.sendLiveStats(player.getUniqueId(), json(player));
                continue;
            }
            Set<Changed> stats = changed.remove(player.getUniqueId());
            StringBuilder out = new StringBuilder("{\"stats\":{");
            appendCustom(out, player);
            if (stats != null) {
                for (Map.Entry<Statistic, String> type : TYPES.entrySet()) {
                    StringBuilder values = new StringBuilder();
                    for (Changed stat : stats) {
                        if (stat.statistic != type.getKey()) continue;
                        int value = stat.material != null ? value(player, stat.statistic, stat.material)
                                : value(player, stat.statistic, stat.entity);
                        if (value <= 0) continue;
                        if (values.length() > 0) values.append(',');
                        values.append('"').append(key(stat)).append("\":").append(value);
                    }
                    if (values.length() > 0) out.append(",\"").append(type.getValue()).append("\":{").append(values).append('}');
                }
            }
            plugin.sendLiveStats(player.getUniqueId(), out.append("}}").toString());
        }
    }

    /** Once a minute: custom stats and all blocks mined and placed. */
    private void sendAll() {
        for (Player player : plugin.getServer().getOnlinePlayers()) {
            plugin.sendLiveStats(player.getUniqueId(), json(player));
        }
    }

    @SuppressWarnings("deprecation")  // EntityType.getName() is the only id available in the 1.13 API
    private static String key(Changed stat) {
        return stat.material != null ? stat.material.getKey().toString() : "minecraft:" + stat.entity.getName();
    }

    private void appendCustom(StringBuilder out, Player player) {
        out.append("\"minecraft:custom\":{");
        boolean first = true;
        for (Map.Entry<Statistic, String> entry : custom.entrySet()) {
            int value = value(player, entry.getKey(), (Material) null);
            if (value <= 0) continue;
            if (!first) out.append(',');
            out.append('"').append(entry.getValue()).append("\":").append(value);
            first = false;
        }
        out.append('}');
    }

    private String json(Player player) {
        StringBuilder out = new StringBuilder("{\"stats\":{");
        appendCustom(out, player);
        out.append(",\"minecraft:mined\":");
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

    private static int value(Player player, Statistic statistic, EntityType entity) {
        try {
            return player.getStatistic(statistic, entity);
        } catch (IllegalArgumentException e) {
            return 0;  // e.g. an entity without statistics
        }
    }
}
