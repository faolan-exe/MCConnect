package org.tobias.mcconnect.spawnelytra;

import com.google.gson.JsonObject;
import net.md_5.bungee.api.ChatMessageType;
import net.md_5.bungee.api.chat.TextComponent;
import org.bukkit.ChatColor;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityToggleGlideEvent;
import org.bukkit.event.player.PlayerGameModeChangeEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerToggleFlightEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;
import org.bukkit.util.Vector;
import org.tobias.mcdatalink.api.Addon;
import org.tobias.mcdatalink.api.AddonConfig;
import org.tobias.mcdatalink.api.AddonHandle;
import org.tobias.mcdatalink.api.MCConnect;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Gliding without an elytra around the spawn (the old SpawnElytra as an MCConnect addon): a double jump in the zone
 * starts a glide, the landing is without fall damage. Fixed against the old plugin: the world is configurable (it was
 * fixed to "world"), only players in the zone get the right to fly and only that right is taken back (it used to
 * switch off /fly of other plugins for every survival player), the right is taken back on quit (the game stores it),
 * no isAir() (1.14+), the action bar is not sent every 3 ticks and players who left are forgotten.
 */
public final class SpawnElytra extends JavaPlugin implements Addon, Listener {
    private static final long TICK_PERIOD = 4L;
    private static final long LANDING_GRACE_MS = 2000L;

    private AddonHandle handle;
    private volatile AddonConfig settings;
    private BukkitTask task;
    private long ticks;
    /** players we gave the right to fly (to start the glide with a double jump) */
    private final Set<UUID> granted = new HashSet<>();
    private final Set<UUID> gliding = new HashSet<>();
    private final Map<UUID, Long> landedAt = new HashMap<>();
    /** who changed the settings in the game and waits for the answer */
    private UUID savedBy;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). SpawnElytra needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(this, this);
        PluginCommand command = getCommand("spawnelytra");
        if (command != null) {
            command.setExecutor(this);
            command.setTabCompleter(this);
        }
    }

    @Override
    public void onDisable() {
        stop();
    }

    @Override
    public void onActivate(AddonConfig config) {
        settings = config;
        if (task == null) task = getServer().getScheduler().runTaskTimer(this, this::tick, TICK_PERIOD, TICK_PERIOD);
        Player actor = savedBy == null ? null : getServer().getPlayer(savedBy);
        if (actor != null) actor.sendMessage(ChatColor.GREEN + "SpawnElytra: gespeichert. " + ChatColor.GRAY + describe(config));
        savedBy = null;
    }

    @Override
    public void onDeactivate() {
        stop();
    }

    private void stop() {
        settings = null;
        if (task != null) {
            task.cancel();
            task = null;
        }
        for (UUID uuid : new ArrayList<>(granted)) {
            Player player = getServer().getPlayer(uuid);
            if (player != null) revoke(player);
        }
        granted.clear();
        gliding.clear();
    }

    // ------------------------------------------------------------------ zone

    private World zoneWorld(AddonConfig config) {
        String name = config.getString("world", "").trim();
        if (name.isEmpty()) return getServer().getWorlds().isEmpty() ? null : getServer().getWorlds().get(0);
        return getServer().getWorld(name);
    }

    private double[] center(AddonConfig config, World world) {
        int[] custom = config.getPosition("center");
        if ("custom".equals(config.getString("center_mode", "spawn")) && custom != null) {
            return new double[]{custom[0] + 0.5, custom[2] + 0.5};
        }
        Location spawn = world.getSpawnLocation();
        return new double[]{spawn.getX(), spawn.getZ()};
    }

    private boolean inZone(Player player, AddonConfig config) {
        World world = zoneWorld(config);
        if (world == null || !player.getWorld().equals(world)) return false;
        double[] center = center(config, world);
        double dx = player.getLocation().getX() - center[0];
        double dz = player.getLocation().getZ() - center[1];
        double radius = config.getDouble("radius", 20);
        return dx * dx + dz * dz <= radius * radius;
    }

    private static boolean eligible(Player player, AddonConfig config) {
        if (!config.getStringList("gamemodes").contains(player.getGameMode().name().toLowerCase(Locale.ROOT))) return false;
        return !config.getBoolean("require_permission", false) || player.hasPermission("spawnelytra.use");
    }

    // ------------------------------------------------------------------ every few ticks

    private void tick() {
        AddonConfig config = settings;
        if (config == null) return;
        boolean showBar = config.getBoolean("actionbar", true) && ticks++ % 5 == 0;  // once a second
        for (Player player : getServer().getOnlinePlayers()) {
            UUID uuid = player.getUniqueId();
            if (gliding.contains(uuid)) {
                if (isOnGround(player) || player.isInsideVehicle() || player.getLocation().getBlock().isLiquid() || player.isDead()) {
                    endGlide(player);
                }
                continue;
            }
            if (eligible(player, config) && inZone(player, config)) {
                if (!player.getAllowFlight()) {
                    player.setAllowFlight(true);
                    granted.add(uuid);
                }
                if (showBar && granted.contains(uuid)) {
                    player.spigot().sendMessage(ChatMessageType.ACTION_BAR,
                            TextComponent.fromLegacyText(color(config.getString("actionbar_text", ""))));
                }
            } else if (granted.contains(uuid)) {
                revoke(player);
            }
        }
    }

    /** Take back only the right we gave, and not from players who switched to creative or spectator meanwhile. */
    private void revoke(Player player) {
        granted.remove(player.getUniqueId());
        GameMode mode = player.getGameMode();
        if (mode == GameMode.SURVIVAL || mode == GameMode.ADVENTURE) {
            player.setFlying(false);
            player.setAllowFlight(false);
        }
    }

    private void endGlide(Player player) {
        gliding.remove(player.getUniqueId());
        landedAt.put(player.getUniqueId(), System.currentTimeMillis());
        if (player.isGliding()) player.setGliding(false);
    }

    @SuppressWarnings("deprecation")  // the server's own on-ground flag is fine for this
    private static boolean isOnGround(Player player) {
        return player.isOnGround();
    }

    // ------------------------------------------------------------------ events

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onToggleFlight(PlayerToggleFlightEvent event) {
        AddonConfig config = settings;
        Player player = event.getPlayer();
        if (config == null || !event.isFlying() || !granted.contains(player.getUniqueId())) return;
        if (!eligible(player, config) || !inZone(player, config)) return;
        event.setCancelled(true);
        revoke(player);  // no second double jump while gliding; given again after the landing
        gliding.add(player.getUniqueId());
        player.setGliding(true);
        double boost = config.getDouble("boost", 1.0);
        if (boost > 0) {
            Vector velocity = player.getLocation().getDirection().normalize().multiply(boost);
            velocity.setY(Math.max(velocity.getY(), 0.3 + 0.2 * boost));
            player.setVelocity(velocity);
        }
        player.spigot().sendMessage(ChatMessageType.ACTION_BAR, new TextComponent(""));
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onToggleGlide(EntityToggleGlideEvent event) {
        if (!(event.getEntity() instanceof Player)) return;
        Player player = (Player) event.getEntity();
        if (event.isGliding() || !gliding.contains(player.getUniqueId())) return;
        if (isOnGround(player) || player.getLocation().getBlock().isLiquid()) endGlide(player);
        else event.setCancelled(true);  // without an elytra the game would end the glide right away
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        AddonConfig config = settings;
        if (config == null || !(event.getEntity() instanceof Player) || !config.getBoolean("no_fall_damage", true)) return;
        if (event.getCause() != EntityDamageEvent.DamageCause.FALL && event.getCause() != EntityDamageEvent.DamageCause.FLY_INTO_WALL) return;
        UUID uuid = event.getEntity().getUniqueId();
        Long landed = landedAt.get(uuid);
        if (gliding.contains(uuid) || (landed != null && System.currentTimeMillis() - landed < LANDING_GRACE_MS)) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGH)
    public void onInteract(PlayerInteractEvent event) {
        AddonConfig config = settings;
        if (config == null || config.getBoolean("allow_rockets", false) || !gliding.contains(event.getPlayer().getUniqueId())) return;
        if (event.getItem() != null && event.getItem().getType().name().equals("FIREWORK_ROCKET")) event.setCancelled(true);
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        Player player = event.getPlayer();
        gliding.remove(player.getUniqueId());
        landedAt.remove(player.getUniqueId());
        if (granted.contains(player.getUniqueId())) revoke(player);  // the game stores the right to fly
    }

    @EventHandler
    public void onGameModeChange(PlayerGameModeChangeEvent event) {
        granted.remove(event.getPlayer().getUniqueId());  // the new mode decides about flying
        gliding.remove(event.getPlayer().getUniqueId());
    }

    // ------------------------------------------------------------------ /spawnelytra

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        AddonConfig config = settings;
        if (config == null) {
            sender.sendMessage(ChatColor.GRAY + "SpawnElytra ist auf diesem Server gerade ausgeschaltet (MCConnect-Website → Erweiterungen).");
            return true;
        }
        String action = args.length == 0 ? "info" : args[0].toLowerCase(Locale.ROOT);
        if (action.equals("info")) {
            sender.sendMessage(ChatColor.GOLD + "SpawnElytra " + ChatColor.GRAY + describe(config));
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra setcenter " + ChatColor.DARK_GRAY + "– Mittelpunkt hierher");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra radius <blöcke> " + ChatColor.DARK_GRAY + "– Radius ändern");
            return true;
        }
        JsonObject changed = config.toJson();
        if (action.equals("setcenter") && sender instanceof Player) {
            Location at = ((Player) sender).getLocation();
            changed.addProperty("center_mode", "custom");
            changed.add("center", AddonConfig.position(at.getBlockX(), at.getBlockY(), at.getBlockZ()));
            World main = getServer().getWorlds().get(0);
            changed.addProperty("world", at.getWorld().equals(main) ? "" : at.getWorld().getName());
        } else if (action.equals("radius") && args.length > 1) {
            try {
                changed.addProperty("radius", Integer.parseInt(args[1]));
            } catch (NumberFormatException e) {
                sender.sendMessage(ChatColor.RED + "Der Radius muss eine Zahl sein.");
                return true;
            }
        } else {
            sender.sendMessage(ChatColor.GRAY + "Benutzung: /spawnelytra [info|setcenter|radius <blöcke>]");
            return true;
        }
        Player actor = sender instanceof Player ? (Player) sender : null;
        if (handle.saveConfig(changed, actor)) {
            savedBy = actor == null ? null : actor.getUniqueId();
            sender.sendMessage(ChatColor.GRAY + "Wird in MCConnect gespeichert …");
        } else {
            sender.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar, nichts geändert.");
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length != 1) return Collections.emptyList();
        List<String> matches = new ArrayList<>();
        for (String option : Arrays.asList("info", "setcenter", "radius")) {
            if (option.startsWith(args[0].toLowerCase(Locale.ROOT))) matches.add(option);
        }
        return matches;
    }

    private String describe(AddonConfig config) {
        World world = zoneWorld(config);
        if (world == null) return "Die Welt »" + config.getString("world", "") + "« gibt es nicht.";
        double[] center = center(config, world);
        return "Radius " + config.getInt("radius", 20) + " um " + (int) Math.floor(center[0]) + " / " + (int) Math.floor(center[1])
                + " in " + world.getName() + ("custom".equals(config.getString("center_mode", "spawn")) ? "" : " (Weltspawn)");
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }
}
