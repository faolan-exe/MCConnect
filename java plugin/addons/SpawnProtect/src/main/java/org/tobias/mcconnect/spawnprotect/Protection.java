package org.tobias.mcconnect.spawnprotect;

import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.entity.ArmorStand;
import org.bukkit.entity.Entity;
import org.bukkit.entity.FallingBlock;
import org.bukkit.entity.Hanging;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.Monster;
import org.bukkit.entity.Player;
import org.bukkit.entity.Projectile;
import org.bukkit.event.Event;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.Action;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockBurnEvent;
import org.bukkit.event.block.BlockExplodeEvent;
import org.bukkit.event.block.BlockFromToEvent;
import org.bukkit.event.block.BlockIgniteEvent;
import org.bukkit.event.block.BlockPistonExtendEvent;
import org.bukkit.event.block.BlockPistonRetractEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.block.BlockSpreadEvent;
import org.bukkit.event.entity.CreatureSpawnEvent;
import org.bukkit.event.entity.EntityChangeBlockEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityExplodeEvent;
import org.bukkit.event.entity.EntityInteractEvent;
import org.bukkit.event.entity.FoodLevelChangeEvent;
import org.bukkit.event.hanging.HangingBreakByEntityEvent;
import org.bukkit.event.hanging.HangingBreakEvent;
import org.bukkit.event.hanging.HangingPlaceEvent;
import org.bukkit.event.player.PlayerArmorStandManipulateEvent;
import org.bukkit.event.player.PlayerBucketEmptyEvent;
import org.bukkit.event.player.PlayerBucketFillEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.projectiles.ProjectileSource;

import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * The protections of the regions. Fixed against the old plugin: explosions outside no longer destroy blocks inside
 * (only the exploding entity's position was checked), only natural monster spawns are stopped (spawn eggs, breeding
 * and spawners of plugins still work), damage is split into PvP, all damage and attacks on animals, and blocks
 * pushed or flowing in from outside are covered too.
 */
final class Protection implements Listener {
    /** spawn reasons of monsters that are stopped ("mobs"); compared by name, some are missing in older versions */
    private static final Set<String> NATURAL_SPAWNS = new HashSet<>(Arrays.asList("NATURAL", "SPAWNER", "REINFORCEMENTS",
            "PATROL", "RAID", "VILLAGE_INVASION", "JOCKEY", "MOUNT", "TRAP"));
    /** monsters that do not implement Monster in every version */
    private static final Set<String> HOSTILE = new HashSet<>(Arrays.asList("SLIME", "MAGMA_CUBE", "PHANTOM", "GHAST", "SHULKER",
            "HOGLIN", "ENDER_DRAGON"));

    private final SpawnProtect plugin;

    Protection(SpawnProtect plugin) {
        this.plugin = plugin;
    }

    private boolean denied(Player player, Location location, String flag) {
        if (!plugin.protects(location, flag) || plugin.bypasses(player)) return false;
        plugin.tellDenied(player);
        return true;
    }

    private static Player playerOf(Entity damager) {
        if (damager instanceof Player) return (Player) damager;
        if (damager instanceof Projectile) {
            ProjectileSource shooter = ((Projectile) damager).getShooter();
            if (shooter instanceof Player) return (Player) shooter;
        }
        return null;
    }

    // ------------------------------------------------------------------ build

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBreak(BlockBreakEvent event) {
        if (denied(event.getPlayer(), event.getBlock().getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onPlace(BlockPlaceEvent event) {
        if (denied(event.getPlayer(), event.getBlock().getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBucketEmpty(PlayerBucketEmptyEvent event) {
        Block target = event.getBlockClicked().getRelative(event.getBlockFace());
        if (denied(event.getPlayer(), target.getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBucketFill(PlayerBucketFillEvent event) {
        if (denied(event.getPlayer(), event.getBlockClicked().getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onHangingPlace(HangingPlaceEvent event) {
        if (event.getPlayer() != null && denied(event.getPlayer(), event.getEntity().getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onHangingBreak(HangingBreakEvent event) {
        Location at = event.getEntity().getLocation();
        if (event instanceof HangingBreakByEntityEvent) {
            Player player = playerOf(((HangingBreakByEntityEvent) event).getRemover());
            if (player != null) {
                if (denied(player, at, "build")) event.setCancelled(true);
                return;
            }
        }
        if (event.getCause() == HangingBreakEvent.RemoveCause.EXPLOSION && plugin.protects(at, "explosions")) event.setCancelled(true);
        if (event.getCause() == HangingBreakEvent.RemoveCause.ENTITY && plugin.protects(at, "mob_grief")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onArmorStand(PlayerArmorStandManipulateEvent event) {
        if (denied(event.getPlayer(), event.getRightClicked().getLocation(), "build")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onInteractEntity(PlayerInteractEntityEvent event) {
        Entity entity = event.getRightClicked();
        if (entity instanceof Hanging && denied(event.getPlayer(), entity.getLocation(), "build")) event.setCancelled(true);
    }

    // ------------------------------------------------------------------ interact, trample

    @EventHandler(priority = EventPriority.HIGH)
    public void onInteract(PlayerInteractEvent event) {
        Block block = event.getClickedBlock();
        if (block == null) return;
        if (event.getAction() == Action.PHYSICAL) {
            if (block.getType() == Material.FARMLAND && plugin.protects(block.getLocation(), "trample") && !plugin.bypasses(event.getPlayer())) {
                event.setCancelled(true);
            }
            return;
        }
        if (event.getAction() == Action.RIGHT_CLICK_BLOCK && block.getType().isInteractable()
                && denied(event.getPlayer(), block.getLocation(), "interact")) {
            event.setUseInteractedBlock(Event.Result.DENY);  // the item in the hand still works (eating, ...)
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onEntityInteract(EntityInteractEvent event) {
        if (event.getBlock().getType() == Material.FARMLAND && plugin.protects(event.getBlock().getLocation(), "trample")) {
            event.setCancelled(true);
        }
    }

    // ------------------------------------------------------------------ damage, hunger

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        Entity victim = event.getEntity();
        Location at = victim.getLocation();
        EntityDamageEvent.DamageCause cause = event.getCause();
        boolean explosion = cause == EntityDamageEvent.DamageCause.BLOCK_EXPLOSION || cause == EntityDamageEvent.DamageCause.ENTITY_EXPLOSION;
        if (victim instanceof Player && plugin.protects(at, "damage")) {
            event.setCancelled(true);
            return;
        }
        if (explosion && !(victim instanceof Monster) && plugin.protects(at, "explosions")) {
            event.setCancelled(true);
            return;
        }
        if (!(event instanceof EntityDamageByEntityEvent)) return;
        Player attacker = playerOf(((EntityDamageByEntityEvent) event).getDamager());
        if (attacker == null || attacker == victim) return;
        if (victim instanceof Player) {
            // protected if the victim or the attacker stands in a region (no shooting from outside in or inside out)
            if ((plugin.protects(at, "pvp") || plugin.protects(attacker.getLocation(), "pvp")) && !plugin.bypasses(attacker)) {
                plugin.tellDenied(attacker);
                event.setCancelled(true);
            }
        } else if (victim instanceof ArmorStand || victim instanceof Hanging) {
            if (denied(attacker, at, "build")) event.setCancelled(true);
        } else if (victim instanceof LivingEntity && !isHostile(victim)) {
            if (denied(attacker, at, "animals")) event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onHunger(FoodLevelChangeEvent event) {
        if (event.getEntity() instanceof Player && event.getFoodLevel() < ((Player) event.getEntity()).getFoodLevel()
                && plugin.protects(event.getEntity().getLocation(), "hunger")) {
            event.setCancelled(true);
        }
    }

    // ------------------------------------------------------------------ mobs

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onSpawn(CreatureSpawnEvent event) {
        if (isHostile(event.getEntity()) && NATURAL_SPAWNS.contains(event.getSpawnReason().name())
                && plugin.protects(event.getLocation(), "mobs")) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onChangeBlock(EntityChangeBlockEvent event) {
        Entity entity = event.getEntity();
        if (entity instanceof Player || entity instanceof FallingBlock) return;
        if (plugin.protects(event.getBlock().getLocation(), "mob_grief")) event.setCancelled(true);
    }

    private static boolean isHostile(Entity entity) {
        return entity instanceof Monster || HOSTILE.contains(entity.getType().name());
    }

    // ------------------------------------------------------------------ explosions, fire, liquids, pistons

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onEntityExplode(EntityExplodeEvent event) {
        event.blockList().removeIf(block -> plugin.protects(block.getLocation(), "explosions"));
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBlockExplode(BlockExplodeEvent event) {
        event.blockList().removeIf(block -> plugin.protects(block.getLocation(), "explosions"));
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onIgnite(BlockIgniteEvent event) {
        if (!plugin.protects(event.getBlock().getLocation(), "fire")) return;
        Player player = event.getPlayer();
        if (player != null && plugin.bypasses(player)) return;
        if (player != null) plugin.tellDenied(player);
        event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onBurn(BlockBurnEvent event) {
        if (plugin.protects(event.getBlock().getLocation(), "fire")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onSpread(BlockSpreadEvent event) {
        if (event.getSource().getType() == Material.FIRE && plugin.protects(event.getBlock().getLocation(), "fire")) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onFlow(BlockFromToEvent event) {
        if (plugin.protects(event.getToBlock().getLocation(), "liquids") && !plugin.protects(event.getBlock().getLocation(), "liquids")) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onPistonExtend(BlockPistonExtendEvent event) {
        if (pushesIn(event.getBlock(), event.getBlocks(), event.getDirection())) event.setCancelled(true);
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onPistonRetract(BlockPistonRetractEvent event) {
        if (pushesIn(event.getBlock(), event.getBlocks(), event.getDirection())) event.setCancelled(true);
    }

    /** A piston outside that would move blocks into, out of or inside a protected region. */
    private boolean pushesIn(Block piston, List<Block> moved, BlockFace direction) {
        if (plugin.protects(piston.getLocation(), "pistons")) return false;
        if (plugin.protects(piston.getRelative(direction).getLocation(), "pistons")) return true;
        for (Block block : moved) {
            if (plugin.protects(block.getLocation(), "pistons") || plugin.protects(block.getRelative(direction).getLocation(), "pistons")) {
                return true;
            }
        }
        return false;
    }
}
