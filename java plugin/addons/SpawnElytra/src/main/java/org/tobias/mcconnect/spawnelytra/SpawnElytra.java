package org.tobias.mcconnect.spawnelytra;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import net.md_5.bungee.api.ChatMessageType;
import net.md_5.bungee.api.chat.BaseComponent;
import net.md_5.bungee.api.chat.ClickEvent;
import net.md_5.bungee.api.chat.ComponentBuilder;
import net.md_5.bungee.api.chat.HoverEvent;
import net.md_5.bungee.api.chat.TextComponent;
import org.bukkit.ChatColor;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Statistic;
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
import org.bukkit.event.player.PlayerStatisticIncrementEvent;
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
 * areas (rectangles and circles, each with its own boost), or a single jump if so set, starts a glide, the landing is without fall damage. Fixed
 * against the old plugin: the world is configurable (it was fixed to "world"), only players in the zone get the right to fly and only that right is taken back (it used to
 * switch off /fly of other plugins for every survival player), the right is taken back on quit (the game stores it),
 * no isAir() (1.14+), the action bar is not sent every 3 ticks and players who left are forgotten.
 */
public final class SpawnElytra extends JavaPlugin implements Addon, Listener {
    private static final long TICK_PERIOD = 4L;
    private static final long LANDING_GRACE_MS = 2000L;
    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9_-]{1,32}");
    private static final double MAX_BOOST = 5.0;
    /** ticks after a jump until the glide starts with a single jump: the player is in the air by then */
    private static final long JUMP_DELAY = 2L;
    private static final String BAR_DOUBLE = "&bSpawnElytra bereit &8| &3zweimal Leertaste zum Fliegen";
    private static final String BAR_SINGLE = "&bSpawnElytra bereit &8| &3Springen zum Fliegen";

    private AddonHandle handle;
    private volatile AddonConfig settings;
    private volatile List<Area> areas = Collections.emptyList();
    /** also the areas switched off, for list and show */
    private volatile List<Area> allAreas = Collections.emptyList();
    private Outline outline;
    /** corners a player has set with pos1 / pos2 */
    private final Map<UUID, Location[]> selections = new HashMap<>();
    private BukkitTask task;
    private long ticks;
    /** players we gave the right to fly (to start the glide with a double jump) */
    private final Set<UUID> granted = new HashSet<>();
    private final Set<UUID> gliding = new HashSet<>();
    private final Map<UUID, Long> landedAt = new HashMap<>();
    /** jumped with a single jump set, the glide starts in a moment */
    private final Set<UUID> jumping = new HashSet<>();
    /** who changed the settings in the game and waits for the answer */
    private UUID savedBy;
    private String savedWhat;
    /** the area to show the menu of after saving, or null */
    private String savedArea;
    /** when the last change went to MCConnect; the next one waits for it (else it would build on the old settings) */
    private long savingSince;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). SpawnElytra needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        outline = new Outline(this);
        Outline.removeLeftovers(getServer());
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
        List<Area> all = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        for (AddonConfig row : config.getList("regions")) {
            all.add(Area.any(row));
            Area area = Area.of(row);
            if (area == null) continue;
            compiled.add(area);
            if (!area.world.isEmpty() && getServer().getWorld(area.world) == null) {
                problems.add("Bereich »" + area.name + "«: die Welt »" + area.world + "« gibt es auf dem Server nicht.");
            }
        }
        areas = Collections.unmodifiableList(compiled);
        allAreas = Collections.unmodifiableList(all);
        handle.reportProblems(problems);
        if (task == null) task = getServer().getScheduler().runTaskTimer(this, this::tick, TICK_PERIOD, TICK_PERIOD);
        Player actor = savedBy == null ? null : getServer().getPlayer(savedBy);
        if (actor != null) {
            actor.sendMessage(ChatColor.GREEN + "SpawnElytra: " + savedWhat);
            Area changed = savedArea == null ? null : find(savedArea);
            if (changed != null) menu(actor, changed);
        }
        savedBy = null;
        savedWhat = null;
        savedArea = null;
        savingSince = 0;
    }

    @Override
    public void onDeactivate() {
        stop();
    }

    private void stop() {
        settings = null;
        areas = Collections.emptyList();
        allAreas = Collections.emptyList();
        if (outline != null) outline.stopAll();
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
        jumping.clear();
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

    /** A single jump starts the glide instead of a double jump (no right to fly needed then). */
    private static boolean singleJump(AddonConfig config) {
        return "single".equals(config.getString("activation", "double"));
    }

    private static String barText(AddonConfig config, boolean single) {
        String text = config.getString("actionbar_text", "");
        return single && text.equals(BAR_DOUBLE) ? BAR_SINGLE : text;
    }

    // ------------------------------------------------------------------ every few ticks

    private void tick() {
        AddonConfig config = settings;
        if (config == null) return;
        long tick = ticks++;
        boolean showBar = config.getBoolean("actionbar", true) && tick % 5 == 0;  // once a second
        if (tick % 3 == 0) {
            World main = getServer().getWorlds().isEmpty() ? null : getServer().getWorlds().get(0);
            outline.draw(allAreas, main, tick % 6 == 0);
        }
        boolean single = singleJump(config);
        for (Player player : getServer().getOnlinePlayers()) {
            UUID uuid = player.getUniqueId();
            if (gliding.contains(uuid)) {
                if (isOnGround(player) || player.isInsideVehicle() || player.getLocation().getBlock().isLiquid() || player.isDead()) {
                    endGlide(player);
                }
                continue;
            }
            if (eligible(player, config) && zoneBoost(player) != null) {
                if (single) {
                    if (granted.contains(uuid)) revoke(player);  // switched from double jump
                } else if (!player.getAllowFlight()) {
                    player.setAllowFlight(true);
                    granted.add(uuid);
                }
                if (showBar && (single || granted.contains(uuid))) {
                    player.spigot().sendMessage(ChatMessageType.ACTION_BAR,
                            TextComponent.fromLegacyText(color(barText(config, single))));
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
        startGlide(player, zone);
    }

    /** Single jump: the jump itself counts, the glide starts a moment later in the air. */
    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onJump(PlayerStatisticIncrementEvent event) {
        if (event.getStatistic() != Statistic.JUMP) return;
        AddonConfig config = settings;
        Player player = event.getPlayer();
        UUID uuid = player.getUniqueId();
        if (config == null || !singleJump(config) || gliding.contains(uuid) || player.isFlying() || player.isInsideVehicle()) return;
        if (!eligible(player, config) || zoneBoost(player) == null || !jumping.add(uuid)) return;
        getServer().getScheduler().runTaskLater(this, () -> {
            jumping.remove(uuid);
            AddonConfig now = settings;
            if (now == null || !player.isOnline() || gliding.contains(uuid) || player.isFlying() || player.isInsideVehicle()) return;
            if (isOnGround(player) || player.getLocation().getBlock().isLiquid() || !eligible(player, now)) return;
            Double zone = zoneBoost(player);
            if (zone != null) startGlide(player, zone);
        }, JUMP_DELAY);
    }

    private void startGlide(Player player, double boost) {
        gliding.add(player.getUniqueId());
        player.setGliding(true);
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
        jumping.remove(player.getUniqueId());
        outline.stop(player.getUniqueId());
        selections.remove(player.getUniqueId());
        if (granted.contains(player.getUniqueId())) revoke(player);  // the game stores the right to fly
    }

    @EventHandler
    public void onGameModeChange(PlayerGameModeChangeEvent event) {
        granted.remove(event.getPlayer().getUniqueId());  // the new mode decides about flying
        gliding.remove(event.getPlayer().getUniqueId());
    }

    // ------------------------------------------------------------------ /spawnelytra

    private static final String USAGE = "/spawnelytra [info|show|list|pos1|pos2|create <name> [schub]|circle <name> <radius> [schub] [höhe]"
            + "|edit <name> <was> [wert]|delete <name>]";

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
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra show " + ChatColor.DARK_GRAY + "– Bereiche mit Namen anzeigen (nur für dich)");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra list " + ChatColor.DARK_GRAY + "– alle Bereiche, zum Anklicken bearbeiten");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra pos1 | pos2 " + ChatColor.DARK_GRAY + "– Ecken eines Rechtecks hierher");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra create <name> [schub] " + ChatColor.DARK_GRAY + "– Rechteck anlegen");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra circle <name> <radius> [schub] [höhe] " + ChatColor.DARK_GRAY + "– Kreis um deine Position");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra edit <name> pos1|pos2|center " + ChatColor.DARK_GRAY + "– Ecke oder Mitte hierher");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra edit <name> boost|radius|height <wert> " + ChatColor.DARK_GRAY + "– ändern, +1 oder -1 relativ");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra edit <name> rename <neu> | on | off " + ChatColor.DARK_GRAY + "– umbenennen, ein-, ausschalten");
            sender.sendMessage(ChatColor.GRAY + "/spawnelytra delete <name> " + ChatColor.DARK_GRAY + "– Bereich löschen");
            return true;
        }
        if (action.equals("show") && sender instanceof Player) {
            Player player = (Player) sender;
            boolean on = args.length > 1 ? !args[1].equalsIgnoreCase("off") : !outline.showing(player);
            outline.set(player, on);
            if (!on) {
                player.sendMessage(ChatColor.GRAY + "SpawnElytra: Bereiche werden nicht mehr angezeigt.");
            } else {
                World main = getServer().getWorlds().get(0);
                boolean here = allAreas.stream().anyMatch(area -> area.inWorld(player.getWorld(), main));
                player.sendMessage(ChatColor.GREEN + "SpawnElytra: Bereiche werden " + Outline.DURATION_MS / 1000 + " Sekunden angezeigt, nur für dich. "
                        + ChatColor.GRAY + (here ? "Grün = aktiv, grau = aus. Noch mal /spawnelytra show blendet sie aus." : "In dieser Welt gibt es keinen Bereich."));
            }
            return true;
        }
        if (action.equals("list")) {
            if (allAreas.isEmpty()) sender.sendMessage(ChatColor.GRAY + "Keine Bereiche. Anlegen mit /spawnelytra create oder circle.");
            for (Area area : allAreas) {
                if (sender instanceof Player) menu((Player) sender, area);
                else sender.sendMessage((area.enabled ? ChatColor.GOLD : ChatColor.GRAY) + area.name + (area.enabled ? "" : " (aus)") + ChatColor.GRAY + " – " + area.size());
            }
            return true;
        }
        if ((action.equals("pos1") || action.equals("pos2")) && sender instanceof Player) {
            setCorner((Player) sender, action.equals("pos1") ? 0 : 1);
            return true;
        }
        boolean ingameOnly = action.equals("create") || action.equals("circle");
        if (!(ingameOnly && sender instanceof Player || action.equals("delete") || action.equals("edit")) || args.length < 2) {
            sender.sendMessage(ChatColor.GRAY + "Benutzung: " + USAGE);
            return true;
        }
        if (System.currentTimeMillis() - savingSince < 5000) {
            sender.sendMessage(ChatColor.GRAY + "Die letzte Änderung wird noch gespeichert, gleich noch mal.");
            return true;
        }
        JsonObject changed = config.toJson();
        savedArea = null;
        String what = action.equals("edit") ? editArea(sender, changed, args) : changeAreas(sender, changed, action, args);
        if (what == null) return true;
        Player actor = sender instanceof Player ? (Player) sender : null;
        if (handle.saveConfig(changed, actor)) {
            savedBy = actor == null ? null : actor.getUniqueId();
            savedWhat = what;
            savingSince = System.currentTimeMillis();
            if (actor != null) outline.extend(actor);
            sender.sendMessage(ChatColor.GRAY + "Wird in MCConnect gespeichert …");
        } else {
            savedArea = null;
            sender.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar, nichts geändert.");
        }
        return true;
    }

    private Area find(String name) {
        for (Area area : allAreas) if (area.name.equalsIgnoreCase(name)) return area;
        return null;
    }

    // ------------------------------------------------------------------ menu in the chat

    /** One area with buttons to click: show, move corner or center here, boost and radius up and down, on/off, rename, delete. */
    private void menu(Player player, Area area) {
        ComponentBuilder line = new ComponentBuilder(area.name).color(area.enabled ? net.md_5.bungee.api.ChatColor.GOLD : net.md_5.bungee.api.ChatColor.GRAY)
                .append(" – " + area.size() + (area.enabled ? "" : " (aus)")).color(net.md_5.bungee.api.ChatColor.GRAY);
        player.spigot().sendMessage(line.create());
        String edit = "/spawnelytra edit " + area.name + " ";
        List<BaseComponent> buttons = new ArrayList<>();
        buttons.add(button("zeigen", "/spawnelytra show on", false, "Alle Bereiche eine Minute lang anzeigen"));
        if (!area.circle) {
            buttons.add(button("Ecke 1 hier", edit + "pos1", false, "Ecke 1 auf deine Position setzen"));
            buttons.add(button("Ecke 2 hier", edit + "pos2", false, "Ecke 2 auf deine Position setzen"));
        } else {
            if (!area.aroundSpawn) buttons.add(button("Mitte hier", edit + "center", false, "Mittelpunkt auf deine Position setzen"));
            buttons.add(button("Radius −5", edit + "radius -5", false, "Radius um 5 Blöcke kleiner"));
            buttons.add(button("Radius +5", edit + "radius +5", false, "Radius um 5 Blöcke größer"));
            buttons.add(button("Höhe …", edit + "height ", true, "Höhe eintippen, 0 = ohne Grenze"));
        }
        buttons.add(button("Schub −", edit + "boost -0.5", false, "Startschub um 0,5 kleiner"));
        buttons.add(button("Schub +", edit + "boost +0.5", false, "Startschub um 0,5 größer"));
        buttons.add(button(area.enabled ? "aus" : "an", edit + (area.enabled ? "off" : "on"), false,
                area.enabled ? "Bereich ausschalten (bleibt gespeichert)" : "Bereich einschalten"));
        buttons.add(button("umbenennen", edit + "rename ", true, "Neuen Namen eintippen"));
        TextComponent delete = button("löschen", "/spawnelytra delete " + area.name, true, "Befehl steht im Chat, Enter löscht den Bereich");
        delete.setColor(net.md_5.bungee.api.ChatColor.RED);
        buttons.add(delete);
        TextComponent row = new TextComponent("  ");
        for (BaseComponent button : buttons) {
            row.addExtra(button);
            row.addExtra(new TextComponent(" "));
        }
        player.spigot().sendMessage(row);
    }

    /** [label]: runs the command, or with suggest puts it into the chat line to complete. */
    @SuppressWarnings("deprecation")  // HoverEvent(Action, BaseComponent[]) is the only one in the 1.13 API
    private static TextComponent button(String label, String command, boolean suggest, String hover) {
        TextComponent button = new TextComponent("[" + label + "]");
        button.setColor(suggest ? net.md_5.bungee.api.ChatColor.YELLOW : net.md_5.bungee.api.ChatColor.AQUA);
        button.setClickEvent(new ClickEvent(suggest ? ClickEvent.Action.SUGGEST_COMMAND : ClickEvent.Action.RUN_COMMAND, command));
        button.setHoverEvent(new HoverEvent(HoverEvent.Action.SHOW_TEXT, new ComponentBuilder(hover).create()));
        return button;
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
        savedArea = name;
        return "Bereich " + name + " angelegt." + flatHint;
    }

    /** /spawnelytra edit <name> <what> [value]: puts the change into the settings; what to tell afterwards, or null. */
    private String editArea(CommandSender sender, JsonObject changed, String[] args) {
        if (args.length < 3) {
            sender.sendMessage(ChatColor.GRAY + "Benutzung: /spawnelytra edit <name> pos1|pos2|center|boost|radius|height|rename|on|off [wert]");
            return null;
        }
        JsonArray rows = changed.has("regions") && changed.get("regions").isJsonArray() ? changed.getAsJsonArray("regions") : new JsonArray();
        int index = indexOf(rows, args[1]);
        if (index < 0) {
            sender.sendMessage(ChatColor.RED + "Den Bereich " + args[1] + " gibt es nicht. " + ChatColor.GRAY + "/spawnelytra list");
            return null;
        }
        JsonObject row = rows.get(index).getAsJsonObject();
        String name = string(row, "name", args[1]);
        String shape = string(row, "shape", "rectangle");
        boolean circle = !shape.equals("rectangle");
        String what = args[2].toLowerCase(Locale.ROOT);
        String value = args.length > 3 ? args[3] : null;
        String result;
        switch (what) {
            case "on":
            case "off":
                row.addProperty("enabled", what.equals("on"));
                result = "Bereich " + name + (what.equals("on") ? " eingeschaltet." : " ausgeschaltet.");
                break;
            case "pos1":
            case "pos2":
            case "center": {
                if (!(sender instanceof Player)) {
                    sender.sendMessage(ChatColor.RED + "Das geht nur im Spiel, mit deiner Position.");
                    return null;
                }
                if (what.equals("center") ? !shape.equals("circle") : circle) {
                    sender.sendMessage(ChatColor.RED + (shape.equals("spawn") ? "Der Bereich liegt um den Weltspawn, er hat keinen eigenen Mittelpunkt."
                            : circle ? "Der Bereich ist ein Kreis: /spawnelytra edit " + name + " center" : "Der Bereich ist ein Rechteck: pos1 oder pos2."));
                    return null;
                }
                Location at = ((Player) sender).getLocation().getBlock().getLocation();
                String world = worldName(at.getWorld());
                if (!circle && !world.equalsIgnoreCase(string(row, "world", "").trim())) {
                    sender.sendMessage(ChatColor.RED + "Der Bereich liegt in einer anderen Welt. Lösche ihn und lege ihn hier neu an.");
                    return null;
                }
                row.addProperty("world", world);
                row.add(what, AddonConfig.position(at.getBlockX(), at.getBlockY(), at.getBlockZ()));
                result = (what.equals("center") ? "Mittelpunkt" : "Ecke " + what.charAt(3)) + " von " + name + ": "
                        + at.getBlockX() + " " + at.getBlockY() + " " + at.getBlockZ() + ".";
                if (!circle && sameHeight(row)) {
                    result += ChatColor.GRAY + " Beide Ecken liegen auf einer Höhe: zum Abspringen muss eine mindestens 2 Blöcke höher sein.";
                }
                break;
            }
            case "boost":
            case "radius":
            case "height": {
                if (!what.equals("boost") && !circle) {
                    sender.sendMessage(ChatColor.RED + "Radius und Höhe gibt es nur bei Kreisen.");
                    return null;
                }
                double min = what.equals("radius") ? 2 : 0;
                double max = what.equals("boost") ? MAX_BOOST : what.equals("radius") ? 500 : 384;
                double current = number(row, what, what.equals("boost") ? 1.0 : what.equals("radius") ? 20 : 0);
                Double next = value == null ? null : changeBy(value, current, min, max);
                if (next == null) {
                    sender.sendMessage(ChatColor.RED + "Eine Zahl von " + plain(min) + " bis " + plain(max) + ", mit + oder - davor relativ (z. B. +1).");
                    return null;
                }
                if (what.equals("boost")) row.addProperty(what, Math.round(next * 10) / 10.0);
                else row.addProperty(what, (int) Math.round(next));
                String label = what.equals("boost") ? "Schub" : what.equals("radius") ? "Radius" : "Höhe";
                result = label + " von " + name + ": " + (what.equals("boost") ? String.valueOf(Math.round(next * 10) / 10.0) : String.valueOf(Math.round(next)))
                        + (what.equals("height") && Math.round(next) == 0 ? " (ohne Grenze)." : ".");
                break;
            }
            case "rename": {
                if (value == null || !NAME.matcher(value).matches()) {
                    sender.sendMessage(ChatColor.RED + "Der neue Name darf nur Buchstaben, Ziffern, _ und - haben (höchstens 32).");
                    return null;
                }
                int other = indexOf(rows, value);
                if (other >= 0 && other != index) {
                    sender.sendMessage(ChatColor.RED + "Den Bereich " + value + " gibt es schon.");
                    return null;
                }
                row.addProperty("name", value);
                result = "Bereich " + name + " heißt jetzt " + value + ".";
                name = value;
                break;
            }
            default:
                sender.sendMessage(ChatColor.GRAY + "Benutzung: /spawnelytra edit <name> pos1|pos2|center|boost|radius|height|rename|on|off [wert]");
                return null;
        }
        changed.add("regions", rows);
        savedArea = name;
        return result;
    }

    private static int indexOf(JsonArray rows, String name) {
        for (int i = 0; i < rows.size(); i++) {
            JsonElement element = rows.get(i);
            if (element.isJsonObject() && string(element.getAsJsonObject(), "name", "").equalsIgnoreCase(name)) return i;
        }
        return -1;
    }

    private static String string(JsonObject row, String key, String fallback) {
        JsonElement element = row.get(key);
        return element != null && element.isJsonPrimitive() ? element.getAsString() : fallback;
    }

    private static double number(JsonObject row, String key, double fallback) {
        JsonElement element = row.get(key);
        try {
            return element != null && element.isJsonPrimitive() ? element.getAsDouble() : fallback;
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    private static boolean sameHeight(JsonObject row) {
        JsonElement a = row.get("pos1"), b = row.get("pos2");
        return a != null && b != null && a.isJsonObject() && b.isJsonObject()
                && number(a.getAsJsonObject(), "y", 0) == number(b.getAsJsonObject(), "y", 1);
    }

    /** "+1" / "-1": relative to the current value and kept in [min, max]; otherwise the number itself, if in [min, max]. */
    static Double changeBy(String text, double current, double min, double max) {
        boolean relative = text.startsWith("+") || text.startsWith("-");
        Double parsed;
        try {
            parsed = Double.parseDouble(text.replace(',', '.'));
        } catch (NumberFormatException e) {
            return null;
        }
        if (parsed.isNaN() || parsed.isInfinite()) return null;
        if (relative) return Math.max(min, Math.min(max, current + parsed));
        return parsed >= min && parsed <= max ? parsed : null;
    }

    private static String plain(double number) {
        return number == Math.rint(number) ? String.valueOf((long) number) : String.valueOf(number);
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
        List<String> options;
        if (args.length == 1) {
            options = Arrays.asList("info", "show", "list", "pos1", "pos2", "create", "circle", "edit", "delete");
        } else if (args.length == 2 && args[0].equalsIgnoreCase("show")) {
            options = Arrays.asList("on", "off");
        } else if (args.length == 2 && (args[0].equalsIgnoreCase("edit") || args[0].equalsIgnoreCase("delete"))) {
            options = new ArrayList<>();
            for (Area area : allAreas) options.add(area.name);
        } else if (args.length == 3 && args[0].equalsIgnoreCase("edit")) {
            options = Arrays.asList("pos1", "pos2", "center", "boost", "radius", "height", "rename", "on", "off");
        } else {
            return Collections.emptyList();
        }
        String typed = args[args.length - 1].toLowerCase(Locale.ROOT);
        List<String> matches = new ArrayList<>();
        for (String option : options) {
            if (option.toLowerCase(Locale.ROOT).startsWith(typed)) matches.add(option);
        }
        return matches;
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }
}
