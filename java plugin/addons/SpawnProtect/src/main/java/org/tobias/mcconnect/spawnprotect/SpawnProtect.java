package org.tobias.mcconnect.spawnprotect;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import net.md_5.bungee.api.ChatMessageType;
import net.md_5.bungee.api.chat.TextComponent;
import org.bukkit.ChatColor;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.Particle;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;
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
 * Protected regions (the old spawnProtect as an MCConnect addon). The regions are part of the addon's settings: edited
 * on the website or with /spawnprotect in the game (the change goes to MCConnect, which stores it and sends the
 * settings back). The protections are in {@link Protection}. The last settings are cached by MCDataLink, so the spawn
 * stays protected while MCConnect is not reachable.
 */
public final class SpawnProtect extends JavaPlugin implements Addon, Listener {
    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9_-]{1,32}");
    private static final List<String> ACTIONS = Arrays.asList("pos1", "pos2", "create", "redefine", "delete", "flag", "list", "info", "show");
    private static final long DENY_COOLDOWN_MS = 3000L;
    private static final int SHOW_SECONDS = 30;
    private static final int MAX_PARTICLES = 800;

    private AddonHandle handle;
    private volatile AddonConfig settings;
    private volatile List<Region> regions = Collections.emptyList();
    private final Map<UUID, Location[]> selections = new HashMap<>();
    private final Map<UUID, Long> lastDenied = new HashMap<>();
    private final Map<UUID, Set<String>> inside = new HashMap<>();
    private final Map<UUID, BukkitTask> showing = new HashMap<>();
    private BukkitTask greetings;
    private UUID savedBy;
    private String savedWhat;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). SpawnProtect needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(new Protection(this), this);
        getServer().getPluginManager().registerEvents(this, this);
        PluginCommand command = getCommand("spawnprotect");
        if (command != null) {
            command.setExecutor(this);
            command.setTabCompleter(this);
        }
    }

    @Override
    public void onActivate(AddonConfig config) {
        List<Region> compiled = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        for (AddonConfig row : config.getList("regions")) {
            Region region = Region.of(row);
            if (region == null) continue;
            compiled.add(region);
            if (!region.world.isEmpty() && getServer().getWorld(region.world) == null) {
                problems.add("Bereich »" + region.name + "«: die Welt »" + region.world + "« gibt es auf dem Server nicht.");
            }
        }
        settings = config;
        regions = Collections.unmodifiableList(compiled);
        handle.reportProblems(problems);
        if (greetings == null) greetings = getServer().getScheduler().runTaskTimer(this, this::greet, 10L, 10L);
        Player actor = savedBy == null ? null : getServer().getPlayer(savedBy);
        if (actor != null) actor.sendMessage(ChatColor.GREEN + "SpawnProtect: " + savedWhat);
        savedBy = null;
        getLogger().info(compiled.size() + " protected regions");
    }

    @Override
    public void onDeactivate() {
        settings = null;
        regions = Collections.emptyList();
        if (greetings != null) {
            greetings.cancel();
            greetings = null;
        }
        inside.clear();
    }

    // ------------------------------------------------------------------ for Protection

    boolean protects(Location location, String flag) {
        List<Region> current = regions;
        if (current.isEmpty() || location.getWorld() == null) return false;
        World main = getServer().getWorlds().get(0);
        for (Region region : current) {
            if (region.protect.contains(flag) && region.contains(location, main)) return true;
        }
        return false;
    }

    boolean bypasses(Player player) {
        AddonConfig config = settings;
        return config != null && config.getBoolean("bypass", true) && player.hasPermission("spawnprotect.bypass");
    }

    void tellDenied(Player player) {
        AddonConfig config = settings;
        String text = config == null ? "" : config.getString("deny_message", "");
        if (text.isEmpty()) return;
        long now = System.currentTimeMillis();
        Long last = lastDenied.get(player.getUniqueId());
        if (last != null && now - last < DENY_COOLDOWN_MS) return;
        lastDenied.put(player.getUniqueId(), now);
        player.spigot().sendMessage(ChatMessageType.ACTION_BAR, TextComponent.fromLegacyText(color(text)));
    }

    /** Twice a second: the greeting of a region a player just entered. */
    private void greet() {
        List<Region> current = regions;
        World main = getServer().getWorlds().get(0);
        for (Player player : getServer().getOnlinePlayers()) {
            Set<String> now = new HashSet<>();
            for (Region region : current) {
                if (region.contains(player.getLocation(), main)) now.add(region.name);
            }
            Set<String> before = inside.put(player.getUniqueId(), now);
            if (before == null) continue;  // just joined: no greeting for where they logged out
            for (Region region : current) {
                if (now.contains(region.name) && !before.contains(region.name) && !region.greeting.isEmpty()) {
                    player.spigot().sendMessage(ChatMessageType.ACTION_BAR, TextComponent.fromLegacyText(color(region.greeting)));
                }
            }
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        UUID uuid = event.getPlayer().getUniqueId();
        selections.remove(uuid);
        lastDenied.remove(uuid);
        inside.remove(uuid);
        BukkitTask task = showing.remove(uuid);
        if (task != null) task.cancel();
    }

    // ------------------------------------------------------------------ /spawnprotect

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        AddonConfig config = settings;
        if (config == null) {
            sender.sendMessage(ChatColor.GRAY + "SpawnProtect ist auf diesem Server gerade ausgeschaltet (MCConnect-Website → Erweiterungen).");
            return true;
        }
        String action = args.length == 0 ? "help" : args[0].toLowerCase(Locale.ROOT);
        Player player = sender instanceof Player ? (Player) sender : null;
        if (player == null && !action.equals("list")) {
            sender.sendMessage("Im Spiel benutzen (nur list geht in der Konsole).");
            return true;
        }
        switch (action) {
            case "pos1":
            case "pos2":
                setCorner(player, action.equals("pos1") ? 0 : 1);
                return true;
            case "list":
                list(sender);
                return true;
            case "info":
                info(player);
                return true;
            case "show":
                show(player, args.length > 1 ? args[1] : null);
                return true;
            case "create":
            case "redefine":
            case "delete":
            case "flag":
                if (args.length < 2) break;
                change(player, config, action, args);
                return true;
            default:
                break;
        }
        help(sender);
        return true;
    }

    private void help(CommandSender sender) {
        sender.sendMessage(ChatColor.GOLD + "SpawnProtect");
        sender.sendMessage(ChatColor.GRAY + "/spawnprotect pos1 | pos2 " + ChatColor.DARK_GRAY + "– Ecken setzen (Block im Blick oder deine Position)");
        sender.sendMessage(ChatColor.GRAY + "/spawnprotect create <name> " + ChatColor.DARK_GRAY + "– Bereich aus den Ecken anlegen");
        sender.sendMessage(ChatColor.GRAY + "/spawnprotect redefine <name> " + ChatColor.DARK_GRAY + "– neue Ecken für einen Bereich");
        sender.sendMessage(ChatColor.GRAY + "/spawnprotect flag <name> <schutz> [an|aus] " + ChatColor.DARK_GRAY + "– Schutz umschalten");
        sender.sendMessage(ChatColor.GRAY + "/spawnprotect delete <name> | list | info | show [name]");
    }

    private void setCorner(Player player, int index) {
        Block target = player.getTargetBlock(null, 6);
        Location at = target != null && target.getType() != Material.AIR && !target.getType().name().endsWith("_AIR")
                ? target.getLocation() : player.getLocation().getBlock().getLocation();
        Location[] selection = selections.computeIfAbsent(player.getUniqueId(), k -> new Location[2]);
        selection[index] = at;
        if (selection[1 - index] != null && !selection[1 - index].getWorld().equals(at.getWorld())) selection[1 - index] = null;
        player.sendMessage(ChatColor.GREEN + "Ecke " + (index + 1) + ": " + ChatColor.WHITE + at.getBlockX() + " " + at.getBlockY() + " " + at.getBlockZ()
                + (selection[0] != null && selection[1] != null ? ChatColor.GRAY + " – jetzt /spawnprotect create <name>" : ""));
    }

    private void list(CommandSender sender) {
        List<Region> current = regions;
        if (current.isEmpty()) {
            sender.sendMessage(ChatColor.GRAY + "Keine aktiven Bereiche.");
            return;
        }
        for (Region region : current) {
            sender.sendMessage(ChatColor.GOLD + region.name + ChatColor.GRAY + " – " + region.size()
                    + (region.world.isEmpty() ? "" : " in " + region.world));
        }
    }

    private void info(Player player) {
        World main = getServer().getWorlds().get(0);
        boolean any = false;
        for (Region region : regions) {
            if (!region.contains(player.getLocation(), main)) continue;
            any = true;
            player.sendMessage(ChatColor.GOLD + region.name + ChatColor.GRAY + " – " + region.size());
            player.sendMessage(ChatColor.GRAY + "Geschützt: " + ChatColor.WHITE + String.join(", ", region.protect));
        }
        if (!any) player.sendMessage(ChatColor.GRAY + "Hier ist kein geschützter Bereich.");
    }

    /** Builds the changed settings and hands them to MCConnect (they apply when they come back). */
    private void change(Player player, AddonConfig config, String action, String[] args) {
        String name = args[1];
        JsonObject changed = config.toJson();
        JsonArray rows = changed.has("regions") && changed.get("regions").isJsonArray() ? changed.getAsJsonArray("regions") : new JsonArray();
        JsonObject row = null;
        int index = -1;
        for (int i = 0; i < rows.size(); i++) {
            JsonElement element = rows.get(i);
            if (element.isJsonObject() && element.getAsJsonObject().has("name")
                    && element.getAsJsonObject().get("name").getAsString().equalsIgnoreCase(name)) {
                row = element.getAsJsonObject();
                index = i;
            }
        }
        String done;
        if (action.equals("create") || action.equals("redefine")) {
            Location[] selection = selections.get(player.getUniqueId());
            if (selection == null || selection[0] == null || selection[1] == null) {
                player.sendMessage(ChatColor.RED + "Setze zuerst beide Ecken mit /spawnprotect pos1 und pos2.");
                return;
            }
            if (action.equals("create")) {
                if (!NAME.matcher(name).matches()) {
                    player.sendMessage(ChatColor.RED + "Der Name darf nur Buchstaben, Ziffern, _ und - haben (höchstens 32).");
                    return;
                }
                if (row != null) {
                    player.sendMessage(ChatColor.RED + "Den Bereich " + name + " gibt es schon (neue Ecken: /spawnprotect redefine " + name + ").");
                    return;
                }
                row = new JsonObject();
                row.addProperty("name", name);
                row.addProperty("enabled", true);
                row.add("protect", AddonConfig.strings(Region.FLAGS));
                row.addProperty("radius", 24);
                row.addProperty("greeting", "");
                rows.add(row);
            } else if (row == null) {
                player.sendMessage(ChatColor.RED + "Den Bereich " + name + " gibt es nicht.");
                return;
            }
            row.addProperty("shape", "cuboid");
            World world = selection[0].getWorld();
            row.addProperty("world", world.equals(getServer().getWorlds().get(0)) ? "" : world.getName());
            row.add("pos1", AddonConfig.position(selection[0].getBlockX(), selection[0].getBlockY(), selection[0].getBlockZ()));
            row.add("pos2", AddonConfig.position(selection[1].getBlockX(), selection[1].getBlockY(), selection[1].getBlockZ()));
            done = "Bereich " + name + (action.equals("create") ? " angelegt." : " hat neue Ecken.");
        } else if (row == null) {
            player.sendMessage(ChatColor.RED + "Den Bereich " + name + " gibt es nicht. " + ChatColor.GRAY + "/spawnprotect list");
            return;
        } else if (action.equals("delete")) {
            rows.remove(index);
            done = "Bereich " + name + " gelöscht.";
        } else {  // flag <name> <flag> [an|aus]
            if (args.length < 3 || !Region.FLAGS.contains(args[2].toLowerCase(Locale.ROOT))) {
                player.sendMessage(ChatColor.RED + "Schutz: " + String.join(", ", Region.FLAGS));
                return;
            }
            String flag = args[2].toLowerCase(Locale.ROOT);
            List<String> flags = new ArrayList<>(new AddonConfig(row).getStringList("protect"));
            boolean on = args.length > 3 ? args[3].equalsIgnoreCase("an") || args[3].equalsIgnoreCase("on") : !flags.contains(flag);
            flags.remove(flag);
            if (on) flags.add(flag);
            row.add("protect", AddonConfig.strings(flags));
            done = "Bereich " + name + ": Schutz " + flag + (on ? " an." : " aus.");
        }
        changed.add("regions", rows);
        if (handle.saveConfig(changed, player)) {
            savedBy = player.getUniqueId();
            savedWhat = done;
            player.sendMessage(ChatColor.GRAY + "Wird in MCConnect gespeichert …");
        } else {
            player.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar, nichts geändert.");
        }
    }

    // ------------------------------------------------------------------ show the borders

    private void show(Player player, String name) {
        World main = getServer().getWorlds().get(0);
        List<Region> chosen = new ArrayList<>();
        for (Region region : regions) {
            if (name != null ? region.name.equalsIgnoreCase(name) : region.contains(player.getLocation(), main)) chosen.add(region);
        }
        Location[] selection = selections.get(player.getUniqueId());
        boolean withSelection = name == null && chosen.isEmpty() && selection != null && selection[0] != null && selection[1] != null;
        if (chosen.isEmpty() && !withSelection) {
            player.sendMessage(ChatColor.GRAY + (name != null ? "Den Bereich " + name + " gibt es nicht." : "Du stehst in keinem Bereich (oder: /spawnprotect show <name>)."));
            return;
        }
        BukkitTask old = showing.remove(player.getUniqueId());
        if (old != null) old.cancel();
        final int[] runs = {0};
        BukkitTask task = getServer().getScheduler().runTaskTimer(this, () -> {
            if (!player.isOnline() || runs[0]++ >= SHOW_SECONDS * 2) {
                BukkitTask self = showing.remove(player.getUniqueId());
                if (self != null) self.cancel();
                return;
            }
            if (withSelection) {
                outline(player, selection[0].getWorld(), Math.min(selection[0].getBlockX(), selection[1].getBlockX()),
                        Math.min(selection[0].getBlockY(), selection[1].getBlockY()), Math.min(selection[0].getBlockZ(), selection[1].getBlockZ()),
                        Math.max(selection[0].getBlockX(), selection[1].getBlockX()), Math.max(selection[0].getBlockY(), selection[1].getBlockY()),
                        Math.max(selection[0].getBlockZ(), selection[1].getBlockZ()));
            }
            for (Region region : chosen) {
                if (!region.inWorld(player.getWorld(), main)) continue;
                if (region.aroundSpawn) circle(player, region.radius);
                else outline(player, player.getWorld(), region.minX, region.minY, region.minZ, region.maxX, region.maxY, region.maxZ);
            }
        }, 0L, 10L);
        showing.put(player.getUniqueId(), task);
        player.sendMessage(ChatColor.GRAY + "Die Grenzen leuchten " + SHOW_SECONDS + " Sekunden lang (nur für dich).");
    }

    /** The 12 edges of the box (outer faces of the blocks), at most MAX_PARTICLES points. */
    private static void outline(Player player, World world, int x1, int y1, int z1, int x2, int y2, int z2) {
        if (!player.getWorld().equals(world)) return;
        double ax = x1, ay = y1, az = z1, bx = x2 + 1, by = y2 + 1, bz = z2 + 1;
        // only the part near the player: a box from bedrock to the sky would be mostly invisible anyway
        double py = player.getLocation().getY();
        ay = Math.max(ay, py - 32);
        by = Math.min(by, py + 32);
        if (ay > by) return;
        double total = 4 * ((bx - ax) + (by - ay) + (bz - az));
        double step = Math.max(1, total / MAX_PARTICLES);
        double[][] corners = {{ax, az}, {bx, az}, {bx, bz}, {ax, bz}};
        for (double[] corner : corners) {
            for (double y = ay; y <= by; y += step) dot(player, corner[0], y, corner[1]);
        }
        for (double y : new double[]{ay, by}) {
            for (double x = ax; x <= bx; x += step) {
                dot(player, x, y, az);
                dot(player, x, y, bz);
            }
            for (double z = az; z <= bz; z += step) {
                dot(player, ax, y, z);
                dot(player, bx, y, z);
            }
        }
    }

    private static void circle(Player player, int radius) {
        Location spawn = player.getWorld().getSpawnLocation();
        double y = player.getLocation().getY() + 0.5;
        int points = Math.min(MAX_PARTICLES, Math.max(24, (int) (2 * Math.PI * radius)));
        for (int i = 0; i < points; i++) {
            double angle = 2 * Math.PI * i / points;
            dot(player, spawn.getX() + radius * Math.cos(angle), y, spawn.getZ() + radius * Math.sin(angle));
        }
    }

    private static void dot(Player player, double x, double y, double z) {
        player.spawnParticle(Particle.END_ROD, x, y, z, 1, 0, 0, 0, 0);
    }

    // ------------------------------------------------------------------ tab completion

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> options = Collections.emptyList();
        if (args.length == 1) {
            options = ACTIONS;
        } else if (args.length == 2 && Arrays.asList("redefine", "delete", "flag", "show").contains(args[0].toLowerCase(Locale.ROOT))) {
            options = new ArrayList<>();
            for (Region region : regions) options.add(region.name);
        } else if (args.length == 3 && args[0].equalsIgnoreCase("flag")) {
            options = Region.FLAGS;
        } else if (args.length == 4 && args[0].equalsIgnoreCase("flag")) {
            options = Arrays.asList("an", "aus");
        }
        String prefix = args.length == 0 ? "" : args[args.length - 1].toLowerCase(Locale.ROOT);
        List<String> matches = new ArrayList<>();
        for (String option : options) {
            if (option.toLowerCase(Locale.ROOT).startsWith(prefix)) matches.add(option);
        }
        return matches;
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }
}
