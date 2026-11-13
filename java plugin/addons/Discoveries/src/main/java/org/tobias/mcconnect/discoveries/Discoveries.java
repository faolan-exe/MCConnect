package org.tobias.mcconnect.discoveries;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.bukkit.ChatColor;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.World;
import org.bukkit.advancement.Advancement;
import org.bukkit.advancement.AdvancementProgress;
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

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Places to discover: the first time a player enters one of the areas, they get a real advancement (toast, chat line,
 * own tab "Entdeckungen"), and MCConnect stores the discovery for the website.
 *
 * The advancement progress is the player's own memory of what they found (the game stores it with the world). A
 * discovery is reported to MCConnect as "found|uuid|place|epoch millis" and kept in plugins/Discoveries/state.json
 * until MCConnect answers "ok|uuid|place", so nothing is lost while MCConnect is not reachable.
 */
public final class Discoveries extends JavaPlugin implements Addon, Listener {
    private static final long TICK_PERIOD = 10L;
    /** ticks between two tries to report the discoveries MCConnect has not confirmed yet */
    private static final long RETRY_TICKS = 20L * 60;
    private static final String ROOT = "root";

    private AddonHandle handle;
    private volatile List<Area> areas = Collections.emptyList();
    private final Map<String, Advancement> advancements = new HashMap<>();
    /** the advancement JSON this plugin loaded, by key: unchanged ones are not loaded again */
    private final Map<String, String> loaded = new HashMap<>();
    /** "uuid|place" -> epoch millis, not confirmed by MCConnect yet */
    private final Map<String, Long> pending = new LinkedHashMap<>();
    private final Map<UUID, Location[]> selections = new HashMap<>();
    private BukkitTask task, retryTask;
    private UUID savedBy;
    private String savedWhat;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). Discoveries needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        loadState();
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(this, this);
        PluginCommand command = getCommand("discovery");
        if (command != null) {
            command.setExecutor(this);
            command.setTabCompleter(this);
        }
        retryTask = getServer().getScheduler().runTaskTimer(this, this::report, RETRY_TICKS, RETRY_TICKS);
    }

    @Override
    public void onDisable() {
        stop();
        if (retryTask != null) retryTask.cancel();
        saveState();
    }

    @Override
    public void onActivate(AddonConfig config) {
        List<Area> compiled = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        Set<String> ids = new HashSet<>();
        for (AddonConfig row : config.getList("areas")) {
            Area area = new Area(row);
            if (!Area.NAME.matcher(area.name).matches()) {
                problems.add("Ort »" + area.title + "«: die Kennung darf nur Buchstaben, Ziffern, _ und - haben.");
                continue;
            }
            if (!ids.add(area.id)) {
                problems.add("Die Kennung »" + area.name + "« gibt es zweimal, nur der erste Ort zählt.");
                continue;
            }
            if (Material.matchMaterial(area.icon) == null) {
                problems.add("Ort »" + area.title + "«: das Icon »" + area.icon + "« kennt diese Minecraft-Version nicht.");
            }
            if (!area.world.isEmpty() && getServer().getWorld(area.world) == null) {
                problems.add("Ort »" + area.title + "«: die Welt »" + area.world + "« gibt es auf dem Server nicht.");
            }
            compiled.add(area);
        }
        problems.addAll(register(compiled));
        List<Area> active = new ArrayList<>();
        for (Area area : compiled) {
            if (area.enabled && advancements.containsKey(area.id)) active.add(area);
        }
        areas = Collections.unmodifiableList(active);
        handle.reportProblems(problems);
        if (task == null) task = getServer().getScheduler().runTaskTimer(this, this::tick, TICK_PERIOD, TICK_PERIOD);
        report();
        Player actor = savedBy == null ? null : getServer().getPlayer(savedBy);
        if (actor != null) actor.sendMessage(ChatColor.GREEN + "Discoveries: " + savedWhat);
        savedBy = null;
        savedWhat = null;
    }

    @Override
    public void onDeactivate() {
        stop();
    }

    /** The advancements stay registered: removing them would drop the players' progress. */
    private void stop() {
        areas = Collections.emptyList();
        if (task != null) {
            task.cancel();
            task = null;
        }
    }

    @Override
    public void onMessage(String message) {
        String[] parts = message.split("\\|");
        if (parts.length == 3 && parts[0].equals("ok") && pending.remove(parts[1] + "|" + parts[2]) != null) saveState();
    }

    // ------------------------------------------------------------------ advancements

    /** Loads the advancements of the places (and the tab) that are new or changed. Returns the problems. */
    @SuppressWarnings("deprecation")  // UnsafeValues is the only way to add advancements at runtime on Spigot and Paper
    private List<String> register(List<Area> places) {
        List<String> problems = new ArrayList<>();
        AdvancementJson json = new AdvancementJson(getServer().getBukkitVersion());
        Map<String, String> wanted = new LinkedHashMap<>();
        wanted.put(ROOT, json.root());
        for (Area area : places) wanted.put(area.id, json.place(key(ROOT).toString(), area));
        for (String id : new ArrayList<>(loaded.keySet())) {
            if (!wanted.containsKey(id)) {  // place deleted in the settings
                try {
                    getServer().getUnsafe().removeAdvancement(key(id));
                } catch (Throwable t) {
                    getLogger().warning("Could not remove the advancement of " + id + ": " + t);
                }
                loaded.remove(id);
                advancements.remove(id);
            }
        }
        for (Map.Entry<String, String> entry : wanted.entrySet()) {
            String id = entry.getKey();
            NamespacedKey key = key(id);
            Advancement existing = getServer().getAdvancement(key);
            if (existing != null && entry.getValue().equals(loaded.get(id))) {
                advancements.put(id, existing);
                continue;
            }
            try {
                if (existing != null) getServer().getUnsafe().removeAdvancement(key);
                Advancement advancement = getServer().getUnsafe().loadAdvancement(key, entry.getValue());
                if (advancement == null) throw new IllegalStateException("not accepted by the server");
                advancements.put(id, advancement);
                loaded.put(id, entry.getValue());
            } catch (Throwable t) {
                getLogger().warning("Could not load the advancement " + key + ": " + t);
                problems.add(id.equals(ROOT) ? "Der Advancement-Tab konnte nicht angelegt werden: " + t.getMessage()
                        : "Ort »" + id + "«: das Advancement konnte nicht angelegt werden (" + t.getMessage() + ").");
                advancements.remove(id);
            }
        }
        saveState();
        return problems;
    }

    private NamespacedKey key(String id) {
        return new NamespacedKey(this, id);
    }

    // ------------------------------------------------------------------ finding places

    private void tick() {
        List<Area> places = areas;
        if (places.isEmpty()) return;
        World main = getServer().getWorlds().isEmpty() ? null : getServer().getWorlds().get(0);
        for (Player player : getServer().getOnlinePlayers()) {
            if (player.getGameMode() == GameMode.SPECTATOR) continue;
            Location at = player.getLocation();
            for (Area area : places) {
                if (area.contains(at, main)) discover(player, area);
            }
        }
    }

    private void discover(Player player, Area area) {
        Advancement advancement = advancements.get(area.id);
        if (advancement == null) return;
        AdvancementProgress progress = player.getAdvancementProgress(advancement);
        if (progress.isDone()) return;
        Advancement root = advancements.get(ROOT);
        if (root != null && !player.getAdvancementProgress(root).isDone()) {
            player.getAdvancementProgress(root).awardCriteria(AdvancementJson.CRITERION);  // the tab, silently
        }
        progress.awardCriteria(AdvancementJson.CRITERION);  // toast and chat line by the game
        pending.put(player.getUniqueId() + "|" + area.id, System.currentTimeMillis());
        saveState();
        report();
    }

    /** Sends the discoveries MCConnect has not confirmed yet. */
    private void report() {
        if (handle == null || pending.isEmpty() || !handle.isActive() || !handle.isConnected()) return;
        for (Map.Entry<String, Long> entry : pending.entrySet()) {
            if (!handle.send("found|" + entry.getKey() + "|" + entry.getValue())) return;
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        selections.remove(event.getPlayer().getUniqueId());
    }

    // ------------------------------------------------------------------ state file

    private File stateFile() {
        return new File(getDataFolder(), "state.json");
    }

    private void loadState() {
        File file = stateFile();
        if (!file.isFile()) return;
        try {
            JsonObject state = new JsonParser().parse(new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8)).getAsJsonObject();
            if (state.has("advancements")) {
                for (Map.Entry<String, JsonElement> entry : state.getAsJsonObject("advancements").entrySet()) {
                    loaded.put(entry.getKey(), entry.getValue().getAsString());
                }
            }
            if (state.has("pending")) {
                for (Map.Entry<String, JsonElement> entry : state.getAsJsonObject("pending").entrySet()) {
                    pending.put(entry.getKey(), entry.getValue().getAsLong());
                }
            }
        } catch (Exception e) {
            getLogger().warning("Could not read " + file + ": " + e);
        }
    }

    private void saveState() {
        JsonObject state = new JsonObject();
        JsonObject json = new JsonObject();
        for (Map.Entry<String, String> entry : loaded.entrySet()) json.addProperty(entry.getKey(), entry.getValue());
        JsonObject open = new JsonObject();
        for (Map.Entry<String, Long> entry : pending.entrySet()) open.addProperty(entry.getKey(), entry.getValue());
        state.add("advancements", json);
        state.add("pending", open);
        try {
            File file = stateFile();
            if (!file.getParentFile().isDirectory() && !file.getParentFile().mkdirs()) throw new IOException("no data folder");
            File temp = new File(file.getParentFile(), "state.json.tmp");
            Files.write(temp.toPath(), state.toString().getBytes(StandardCharsets.UTF_8));
            Files.move(temp.toPath(), file.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            getLogger().warning("Could not save the state: " + e);
        }
    }

    // ------------------------------------------------------------------ /discovery

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (handle == null || !handle.isActive()) {
            sender.sendMessage(ChatColor.GRAY + "Discoveries ist auf diesem Server gerade ausgeschaltet (MCConnect-Website → Erweiterungen).");
            return true;
        }
        String action = args.length == 0 ? "info" : args[0].toLowerCase(Locale.ROOT);
        if (action.equals("info")) {
            sender.sendMessage(ChatColor.GOLD + "Discoveries " + ChatColor.GRAY + areas.size() + (areas.size() == 1 ? " aktiver Ort" : " aktive Orte"));
            sender.sendMessage(ChatColor.GRAY + "/discovery pos1 | pos2 " + ChatColor.DARK_GRAY + "– Ecken eines Rechtecks hierher");
            sender.sendMessage(ChatColor.GRAY + "/discovery create <name> " + ChatColor.DARK_GRAY + "– Rechteck anlegen");
            sender.sendMessage(ChatColor.GRAY + "/discovery circle <name> <radius> [höhe] " + ChatColor.DARK_GRAY + "– Kreis um deine Position");
            sender.sendMessage(ChatColor.GRAY + "/discovery delete <name> | list " + ChatColor.DARK_GRAY + "– Ort löschen, alle zeigen");
            return true;
        }
        if (action.equals("list")) {
            if (areas.isEmpty()) sender.sendMessage(ChatColor.GRAY + "Keine aktiven Orte.");
            for (Area area : areas) sender.sendMessage(ChatColor.GOLD + area.name + ChatColor.GRAY + " »" + area.title + "« – " + area.size());
            return true;
        }
        if ((action.equals("pos1") || action.equals("pos2")) && sender instanceof Player) {
            setCorner((Player) sender, action.equals("pos1") ? 0 : 1);
            return true;
        }
        boolean ingameOnly = action.equals("create") || action.equals("circle");
        if (!(ingameOnly && sender instanceof Player || action.equals("delete")) || args.length < 2) {
            sender.sendMessage(ChatColor.GRAY + "Benutzung: /discovery [info|pos1|pos2|create <name>|circle <name> <radius> [höhe]|delete <name>|list]");
            return true;
        }
        JsonObject changed = handle.config().toJson();
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
                + (selection[0] != null && selection[1] != null ? ChatColor.GRAY + " – jetzt /discovery create <name>" : ""));
    }

    /** Puts the change into the settings; what to tell afterwards, or null if it was refused (and said why). */
    private String changeAreas(CommandSender sender, JsonObject changed, String action, String[] args) {
        String name = args[1];
        JsonArray rows = changed.has("areas") && changed.get("areas").isJsonArray() ? changed.getAsJsonArray("areas") : new JsonArray();
        int index = -1;
        for (int i = 0; i < rows.size(); i++) {
            JsonElement element = rows.get(i);
            if (element.isJsonObject() && element.getAsJsonObject().has("name")
                    && element.getAsJsonObject().get("name").getAsString().equalsIgnoreCase(name)) index = i;
        }
        if (action.equals("delete")) {
            if (index < 0) {
                sender.sendMessage(ChatColor.RED + "Den Ort " + name + " gibt es nicht. " + ChatColor.GRAY + "/discovery list");
                return null;
            }
            rows.remove(index);
            changed.add("areas", rows);
            return "Ort " + name + " gelöscht.";
        }
        if (!Area.NAME.matcher(name).matches()) {
            sender.sendMessage(ChatColor.RED + "Der Name darf nur Buchstaben, Ziffern, _ und - haben (höchstens 32).");
            return null;
        }
        if (index >= 0) {
            sender.sendMessage(ChatColor.RED + "Den Ort " + name + " gibt es schon.");
            return null;
        }
        Player player = (Player) sender;
        JsonObject row = new JsonObject();
        row.addProperty("name", name);
        row.addProperty("title", name);
        row.addProperty("description", "");
        row.addProperty("icon", "filled_map");
        row.addProperty("enabled", true);
        if (action.equals("circle")) {
            Integer radius = args.length > 2 ? number(args[2], 2, 1000) : null;
            Integer height = args.length > 3 ? number(args[3], 0, 384) : Integer.valueOf(0);
            if (radius == null || height == null) {
                sender.sendMessage(ChatColor.RED + "Radius 2 bis 1000, Höhe 0 bis 384 (0 = ohne Grenze).");
                return null;
            }
            Location at = player.getLocation();
            row.addProperty("shape", "circle");
            row.addProperty("world", worldName(at.getWorld()));
            row.add("center", AddonConfig.position(at.getBlockX(), at.getBlockY(), at.getBlockZ()));
            row.addProperty("radius", radius);
            row.addProperty("height", height);
        } else {
            Location[] selection = selections.get(player.getUniqueId());
            if (selection == null || selection[0] == null || selection[1] == null) {
                sender.sendMessage(ChatColor.RED + "Setze zuerst beide Ecken mit /discovery pos1 und pos2.");
                return null;
            }
            row.addProperty("shape", "rectangle");
            row.addProperty("world", worldName(selection[0].getWorld()));
            row.add("pos1", AddonConfig.position(selection[0].getBlockX(), selection[0].getBlockY(), selection[0].getBlockZ()));
            row.add("pos2", AddonConfig.position(selection[1].getBlockX(), selection[1].getBlockY(), selection[1].getBlockZ()));
        }
        rows.add(row);
        changed.add("areas", rows);
        return "Ort " + name + " angelegt. Titel, Text und Icon stellst du auf der MCConnect-Website ein.";
    }

    private String worldName(World world) {
        return world.equals(getServer().getWorlds().get(0)) ? "" : world.getName();
    }

    /** The whole number in [min, max], or null. */
    private static Integer number(String text, int min, int max) {
        try {
            int value = Integer.parseInt(text);
            return value >= min && value <= max ? Integer.valueOf(value) : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> matches = new ArrayList<>();
        if (args.length == 1) {
            for (String option : Arrays.asList("info", "pos1", "pos2", "create", "circle", "delete", "list")) {
                if (option.startsWith(args[0].toLowerCase(Locale.ROOT))) matches.add(option);
            }
        } else if (args.length == 2 && args[0].equalsIgnoreCase("delete")) {
            for (Area area : areas) if (area.name.toLowerCase(Locale.ROOT).startsWith(args[1].toLowerCase(Locale.ROOT))) matches.add(area.name);
        }
        return matches;
    }
}
