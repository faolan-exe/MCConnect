package org.tobias.mcconnect.spawnelytra;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
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
import java.util.regex.Pattern;

/**
 * Gliding without an elytra around the spawn (the old SpawnElytra as an MCConnect addon): a double jump in one of the
 * areas (rectangles and circles, each with its own boost) starts a glide, the landing is without fall damage. Fixed
 * against the old plugin: the world is configurable (it was fixed to "world"), only players in the zone get the right to fly and only that right is taken back (it used to
 * switch off /fly of other plugins for every survival player), the right is taken back on quit (the game stores it),
 * no isAir() (1.14+), the action bar is not sent every 3 ticks and players who left are forgotten.
 */
public final class SpawnElytra extends JavaPlugin implements Addon, Listener {
    private static final long TICK_PERIOD = 4L;
    private static final long LANDING_GRACE_MS = 2000L;
    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9_-]{1,32}");
    private static final double MAX_BOOST = 5.0;

    private AddonHandle handle;
    private volatile AddonConfig settings;
    private volatile List<Area> areas = Collections.emptyList();
    /** corners a player has set with pos1 / pos2 */
    private final Map<UUID, Location[]> selections = new HashMap<>();
    private BukkitTask task;
    private long ticks;
    /** players we gave the right to fly (to start the glide with a double jump) */
    private final Set<UUID> granted = new HashSet<>();
    private final Set<UUID> gliding = new HashSet<>();
    private final Map<UUID, Long> landedAt = new HashMap<>();
    /** who changed the settings in the game and waits for the answer */
    private UUID savedBy;
    private String savedWhat;

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
        List<Area> compiled = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        for (AddonConfig row : config.getList("regions")) {
            Area area = Area.of(row);
            if (area == null) continue;
            compiled.add(area);
            if (!area.world.isEmpty() && getServer().getWorld(area.world) == null) {
                problems.add("Bereich »" + area.name + "«: die Welt »" + area.world + "« gibt es auf dem Server nicht.");
            }
        }
        areas = Collections.unmodifiableList(compiled);
        handle.reportProblems(problems);
        if (task == null) task = getServer().getScheduler().runTaskTimer(this, this::tick, TICK_PERIOD, TICK_PERIOD);
        Player actor = savedBy == null ? null : getServer().getPlayer(savedBy);
        if (actor != null) actor.sendMessage(ChatColor.GREEN + "SpawnElytra: " + savedWhat);
        savedBy = null;
        savedWhat = null;
    }

    @Override
    public void onDeactivate() {
        stop();
    }

    private void stop() {
        settings = null;
        areas = Collections.emptyList();
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

    /** The boost at the player's place, or null outside of all areas. The first area of the list wins. */
    private Double zoneBoost(Player player) {
        World main = getServer().getWorlds().isEmpty() ? null : getServer().getWorlds().get(0);
        for (Area area : areas) {
            if (area.contains(player.getLocation(), main)) return area.boost;
        }
        return null;
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
            if (eligible(player, config) && zoneBoost(player) != null) {
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
        Double zone = zoneBoost(player);
        if (!eligible(player, config) || zone == null) return;
        event.setCancelled(true);
        revoke(player);  // no second double jump while gliding; given again after the landing
        gliding.add(player.getUniqueId());
        player.setGliding(true);
        double boost = zone;
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
        selections.remove(player.getUniqueId());
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
            sender.sendMessage(ChatColor.GOLD + "SpawnElytra " + ChatColor.GRAY + areas.size() + (areas.size() == 1 ? " aktiver Bereich" : " aktive Bereiche"));
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra pos1 | pos2 " + ChatColor.DARK_GRAY + "– Ecken eines Rechtecks hierher");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra create <name> [schub] " + ChatColor.DARK_GRAY + "– Rechteck anlegen");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra circle <name> <radius> [schub] [höhe] " + ChatColor.DARK_GRAY + "– Kreis um deine Position");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra delete <name> | list " + ChatColor.DARK_GRAY + "– Bereich löschen, alle zeigen");
            return true;
        }
        if (action.equals("list")) {
            if (areas.isEmpty()) sender.sendMessage(ChatColor.GRAY + "Keine aktiven Bereiche.");
            for (Area area : areas) sender.sendMessage(ChatColor.GOLD + area.name + ChatColor.GRAY + " – " + area.size());
            return true;
        }
        if ((action.equals("pos1") || action.equals("pos2")) && sender instanceof Player) {
            setCorner((Player) sender, action.equals("pos1") ? 0 : 1);
            return true;
        }
        boolean ingameOnly = action.equals("create") || action.equals("circle");
        if (!(ingameOnly && sender instanceof Player || action.equals("delete")) || args.length < 2) {
            sender.sendMessage(ChatColor.GRAY + "Benutzung: /spawnelytra [info|pos1|pos2|create <name> [schub]|circle <name> <radius> [schub] [höhe]|delete <name>|list]");
            return true;
        }
        JsonObject changed = config.toJson();
        String what = changeAreas(sender, changed, action, args);
        if (what == null) return true;
        Player actor = sender instanceof Player ? (Player) sender : null;
        if (handle.saveConfig(changed, actor)) {
            savedBy = actor == null ? null : actor.getUniqueId();
            savedWhat = what;
            sender.sendMessage(ChatColor.GRAY + "Wird in MCConnect gespeichert …");
        } else {
            sender.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar, nichts geändert.");
        }
        return true;
    }

    private void setCorner(Player player, int index) {
        Location at = player.getLocation().getBlock().getLocation();
        Location[] selection = selections.computeIfAbsent(player.getUniqueId(), k -> new Location[2]);
        selection[index] = at;
        if (selection[1 - index] != null && !selection[1 - index].getWorld().equals(at.getWorld())) selection[1 - index] = null;
        player.sendMessage(ChatColor.GREEN + "Ecke " + (index + 1) + ": " + ChatColor.WHITE + at.getBlockX() + " " + at.getBlockY() + " " + at.getBlockZ()
                + (selection[0] != null && selection[1] != null ? ChatColor.GRAY + " – jetzt /spawnelytra create <name> [schub]" : ""));
    }

    /** Puts the change into the settings; what to tell afterwards, or null if it was refused (and said why). */
    private String changeAreas(CommandSender sender, JsonObject changed, String action, String[] args) {
        String name = args[1];
        JsonArray rows = changed.has("regions") && changed.get("regions").isJsonArray() ? changed.getAsJsonArray("regions") : new JsonArray();
        int index = -1;
        for (int i = 0; i < rows.size(); i++) {
            JsonElement element = rows.get(i);
            if (element.isJsonObject() && element.getAsJsonObject().has("name")
                    && element.getAsJsonObject().get("name").getAsString().equalsIgnoreCase(name)) index = i;
        }
        if (action.equals("delete")) {
            if (index < 0) {
                sender.sendMessage(ChatColor.RED + "Den Bereich " + name + " gibt es nicht. " + ChatColor.GRAY + "/spawnelytra list");
                return null;
            }
            rows.remove(index);
            changed.add("regions", rows);
            return "Bereich " + name + " gelöscht.";
        }
        if (!NAME.matcher(name).matches()) {
            sender.sendMessage(ChatColor.RED + "Der Name darf nur Buchstaben, Ziffern, _ und - haben (höchstens 32).");
            return null;
        }
        if (index >= 0) {
            sender.sendMessage(ChatColor.RED + "Den Bereich " + name + " gibt es schon.");
            return null;
        }
        Player player = (Player) sender;
        boolean circle = action.equals("circle");
        JsonObject row = new JsonObject();
        row.addProperty("name", name);
        row.addProperty("enabled", true);
        String flatHint = "";
        if (circle) {
            Double radius = args.length > 2 ? number(args[2], 2, 500) : null;
            Double boost = args.length > 3 ? number(args[3], 0, MAX_BOOST) : Double.valueOf(1.0);
            Double height = args.length > 4 ? number(args[4], 0, 384) : Double.valueOf(0);
            if (radius == null || boost == null || height == null) {
                sender.sendMessage(ChatColor.RED + "Radius 2 bis 500, Schub 0 bis 5, Höhe 0 bis 384 (0 = ohne Grenze).");
                return null;
            }
            Location at = player.getLocation();
            row.addProperty("shape", "circle");
            row.addProperty("world", worldName(at.getWorld()));
            row.add("center", AddonConfig.position(at.getBlockX(), at.getBlockY(), at.getBlockZ()));
            row.addProperty("radius", radius.intValue());
            row.addProperty("height", height.intValue());
            row.addProperty("boost", boost);
        } else {
            Location[] selection = selections.get(player.getUniqueId());
            if (selection == null || selection[0] == null || selection[1] == null) {
                sender.sendMessage(ChatColor.RED + "Setze zuerst beide Ecken mit /spawnelytra pos1 und pos2.");
                return null;
            }
            Double boost = args.length > 2 ? number(args[2], 0, MAX_BOOST) : Double.valueOf(1.0);
            if (boost == null) {
                sender.sendMessage(ChatColor.RED + "Der Schub muss eine Zahl von 0 bis 5 sein.");
                return null;
            }
            row.addProperty("shape", "rectangle");
            row.addProperty("world", worldName(selection[0].getWorld()));
            row.add("pos1", AddonConfig.position(selection[0].getBlockX(), selection[0].getBlockY(), selection[0].getBlockZ()));
            row.add("pos2", AddonConfig.position(selection[1].getBlockX(), selection[1].getBlockY(), selection[1].getBlockZ()));
            row.addProperty("boost", boost);
            if (selection[0].getBlockY() == selection[1].getBlockY()) {
                flatHint = ChatColor.GRAY + " Beide Ecken liegen auf einer Höhe: zum Abspringen muss eine mindestens 2 Blöcke höher sein.";
            }
        }
        rows.add(row);
        changed.add("regions", rows);
        return "Bereich " + name + " angelegt." + flatHint;
    }

    private String worldName(World world) {
        return world.equals(getServer().getWorlds().get(0)) ? "" : world.getName();
    }

    /** The number in [min, max], or null. */
    private static Double number(String text, double min, double max) {
        try {
            double value = Double.parseDouble(text);
            return value >= min && value <= max ? Double.valueOf(value) : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        if (args.length != 1) return Collections.emptyList();
        List<String> matches = new ArrayList<>();
        for (String option : Arrays.asList("info", "pos1", "pos2", "create", "circle", "delete", "list")) {
            if (option.startsWith(args[0].toLowerCase(Locale.ROOT))) matches.add(option);
        }
        return matches;
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }
}
