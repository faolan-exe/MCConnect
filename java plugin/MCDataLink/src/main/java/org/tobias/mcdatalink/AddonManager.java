package org.tobias.mcdatalink;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.server.PluginDisableEvent;
import org.bukkit.plugin.Plugin;
import org.bukkit.plugin.java.JavaPlugin;
import org.tobias.mcdatalink.api.Addon;
import org.tobias.mcdatalink.api.AddonConfig;
import org.tobias.mcdatalink.api.AddonHandle;
import org.tobias.mcdatalink.api.ChatLine;
import org.tobias.mcdatalink.api.MCConnect;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.logging.Level;
import java.util.regex.Pattern;

/**
 * Addons: other plugins that register with {@link MCConnect#register} (plugin 3.22). MCConnect decides which of them
 * may work (the owner approves them on the website, official store addons are approved by their checksum) and holds
 * their settings. Everything here runs on the main thread; messages from the connection thread are handed over.
 *
 * MCConnect learns the addons from !ADDONS (id, plugin, version, SHA-256 of the jar, mcconnect-addon.json, commands)
 * and answers each with !addonstate (active 0/1 and the settings). The last state is cached in
 * plugins/MCDataLink/addons/, so an approved addon also works after a restart while MCConnect is not reachable.
 * Messages of an addon that is not active are dropped in both directions.
 */
final class AddonManager implements MCConnect.Registry, Listener {
    static final Pattern ID = Pattern.compile("[a-z][a-z0-9_]{1,39}");
    private static final String MANIFEST = "mcconnect-addon.json";
    private static final int MAX_PROBLEMS = 20;
    private static final int MAX_PROBLEM_LENGTH = 200;

    private final MCDataLink plugin;
    private final File cacheDir;
    private final Map<String, Registered> addons = new LinkedHashMap<>();
    private boolean reportScheduled;

    AddonManager(MCDataLink plugin) {
        this.plugin = plugin;
        this.cacheDir = new File(plugin.getDataFolder(), "addons");
    }

    // ------------------------------------------------------------------ registration

    @Override
    public AddonHandle register(JavaPlugin owner, Addon addon) {
        if (!plugin.getServer().isPrimaryThread()) throw new IllegalStateException("register on the main thread");
        if (owner == null || addon == null) throw new IllegalArgumentException("plugin and addon are required");
        // Only the plugin itself may register as itself: its addon class must come from its own jar.
        if (addon.getClass().getClassLoader() != owner.getClass().getClassLoader()) {
            throw new IllegalArgumentException("the addon class is not part of " + owner.getName());
        }
        JsonObject manifest = readManifest(owner);
        String id = manifest.get("id").getAsString();
        Registered existing = addons.get(id);
        if (existing != null && existing.owner != owner) {
            throw new IllegalArgumentException("addon id " + id + " is already used by " + existing.owner.getName());
        }
        File jar = jarOf(owner, plugin.getDataFolder().getParentFile());
        Registered registered = new Registered(owner, addon, id, manifest, jar, jar == null ? "" : sha256(jar));
        addons.put(id, registered);
        plugin.getLogger().info("Addon " + id + " (" + owner.getName() + " " + owner.getDescription().getVersion() + ") registered");
        JsonObject cached = readCache(id);
        if (cached != null && cached.has("active") && cached.get("active").getAsBoolean()
                && registered.sha256.equals(string(cached, "sha256"))) {
            // until MCConnect answers: the last approved settings, so e.g. a protection works right after the start
            plugin.getServer().getScheduler().runTask(plugin, () -> {
                if (!registered.stateFromServer && addons.get(id) == registered) {
                    JsonElement config = cached.get("config");
                    registered.apply(true, new AddonConfig(config != null && config.isJsonObject() ? config.getAsJsonObject() : null));
                }
            });
        }
        scheduleReport();
        return registered;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPluginDisable(PluginDisableEvent event) {
        boolean changed = addons.values().removeIf(r -> r.owner == event.getPlugin());
        if (changed) {
            updateCommands();
            scheduleReport();
        }
    }

    Registered byId(String id) {
        return addons.get(id);
    }

    Registered byPlugin(Plugin owner) {
        for (Registered registered : addons.values()) {
            if (registered.owner == owner) return registered;
        }
        return null;
    }

    // ------------------------------------------------------------------ MCConnect

    /** After connecting (and on every change): which addons are there. */
    void report() {
        reportScheduled = false;
        if (!plugin.isConnected()) return;
        JsonArray list = new JsonArray();
        for (Registered registered : addons.values()) {
            JsonObject entry = new JsonObject();
            entry.addProperty("id", registered.id);
            entry.addProperty("plugin", registered.owner.getName());
            entry.addProperty("version", registered.owner.getDescription().getVersion());
            entry.addProperty("file", registered.jar == null ? "" : registered.jar.getName());
            entry.addProperty("sha256", registered.sha256);
            entry.add("manifest", registered.manifest);
            entry.add("commands", commandsOf(registered.owner));
            list.add(entry);
        }
        plugin.sendToMCConnect("!ADDONS~" + list);
        for (Registered registered : addons.values()) {
            if (!registered.problems.isEmpty()) registered.sendProblems();
        }
    }

    private void scheduleReport() {
        if (reportScheduled || !plugin.isEnabled()) return;
        reportScheduled = true;
        plugin.getServer().getScheduler().runTask(plugin, this::report);  // several registrations at the start: one message
    }

    /** From the connection thread: !addonstate~id|active 0/1|settings json. */
    void receiveState(String value) {
        String[] parts = value.split("\\|", 3);
        if (parts.length < 3) return;
        boolean active = "1".equals(parts[1]);
        AddonConfig config = AddonConfig.parse(parts[2]);
        plugin.runOnMainThread(() -> {
            Registered registered = addons.get(parts[0]);
            if (registered == null) return;
            registered.stateFromServer = true;
            writeCache(registered, active, config);
            registered.apply(active, config);
        });
    }

    /** From the connection thread: !addon~id|message. */
    void receiveMessage(String value) {
        int separator = value.indexOf('|');
        if (separator < 0) return;
        String id = value.substring(0, separator);
        String message = value.substring(separator + 1);
        plugin.runOnMainThread(() -> {
            Registered registered = addons.get(id);
            if (registered == null || !registered.active) return;
            registered.call("onMessage", () -> registered.addon.onMessage(message));
        });
    }

    /** Commands of active addons may be run by chat buttons (ChatMarkup.isOwnCommand). */
    private void updateCommands() {
        Set<String> names = new HashSet<>();
        for (Registered registered : addons.values()) {
            if (!registered.active) continue;
            for (JsonElement command : commandsOf(registered.owner)) {
                JsonObject object = command.getAsJsonObject();
                names.add(object.get("name").getAsString().toLowerCase(Locale.ROOT));
                for (JsonElement alias : object.getAsJsonArray("aliases")) names.add(alias.getAsString().toLowerCase(Locale.ROOT));
            }
        }
        ChatMarkup.setAddonCommands(names);
    }

    // ------------------------------------------------------------------ helpers

    /** The addon's mcconnect-addon.json with a valid "id" and "name". */
    static JsonObject readManifest(JavaPlugin owner) {
        try (InputStream in = owner.getResource(MANIFEST)) {
            if (in == null) throw new IllegalArgumentException(owner.getName() + " has no " + MANIFEST);
            return checkManifest(parseObject(new InputStreamReader(in, StandardCharsets.UTF_8)), owner.getName());
        } catch (IOException e) {
            throw new IllegalArgumentException(owner.getName() + ": " + MANIFEST + " cannot be read: " + e.getMessage());
        }
    }

    static JsonObject checkManifest(JsonObject manifest, String source) {
        if (manifest == null || !ID.matcher(string(manifest, "id")).matches() || string(manifest, "name").isEmpty()) {
            throw new IllegalArgumentException(source + ": " + MANIFEST + " needs an \"id\" (a-z, 0-9, _) and a \"name\"");
        }
        return manifest;
    }

    static JsonObject parseObject(Reader reader) {
        try {
            JsonElement element = new JsonParser().parse(reader);
            return element.isJsonObject() ? element.getAsJsonObject() : null;
        } catch (RuntimeException e) {
            return null;
        }
    }

    private static String string(JsonObject object, String key) {
        JsonElement value = object.get(key);
        return value != null && value.isJsonPrimitive() ? value.getAsString() : "";
    }

    /** name, description, usage and aliases of the plugin's commands (shown on the website). */
    private static JsonArray commandsOf(Plugin owner) {
        JsonArray result = new JsonArray();
        Map<String, Map<String, Object>> commands = owner.getDescription().getCommands();
        if (commands == null) return result;
        for (Map.Entry<String, Map<String, Object>> command : commands.entrySet()) {
            Map<String, Object> info = command.getValue() == null ? Collections.emptyMap() : command.getValue();
            JsonObject entry = new JsonObject();
            entry.addProperty("name", command.getKey());
            entry.addProperty("description", String.valueOf(info.getOrDefault("description", "")));
            entry.addProperty("usage", String.valueOf(info.getOrDefault("usage", "")).replace("<command>", command.getKey()));
            JsonArray aliases = new JsonArray();
            Object value = info.get("aliases");
            if (value instanceof String) aliases.add((String) value);
            else if (value instanceof Collection) for (Object alias : (Collection<?>) value) aliases.add(String.valueOf(alias));
            entry.add("aliases", aliases);
            result.add(entry);
        }
        return result;
    }

    /**
     * The plugin's jar in the plugins folder. Paper loads plugins from a remapped copy (plugins/.paper-remapped/),
     * whose checksum and name are not the original's: then the jar in the plugins folder with the same plugin name.
     */
    static File jarOf(Plugin owner, File pluginsFolder) {
        File loaded;
        try {
            loaded = new File(owner.getClass().getProtectionDomain().getCodeSource().getLocation().toURI());
        } catch (URISyntaxException | RuntimeException e) {
            loaded = null;
        }
        if (loaded != null && loaded.isFile() && loaded.getParentFile() != null
                && loaded.getParentFile().getAbsoluteFile().toPath().normalize().equals(pluginsFolder.getAbsoluteFile().toPath().normalize())) {
            return loaded;
        }
        File[] jars = pluginsFolder.listFiles((dir, name) -> name.endsWith(".jar"));
        if (jars != null) {
            for (File jar : jars) {
                if (owner.getName().equalsIgnoreCase(pluginName(jar))) return jar;
            }
        }
        return loaded != null && loaded.isFile() ? loaded : null;
    }

    private static String pluginName(File jar) {
        try (java.util.zip.ZipFile zip = new java.util.zip.ZipFile(jar)) {
            java.util.zip.ZipEntry entry = zip.getEntry("plugin.yml");
            if (entry == null) return null;
            try (InputStream in = zip.getInputStream(entry)) {
                return new org.bukkit.plugin.PluginDescriptionFile(in).getName();
            }
        } catch (IOException | org.bukkit.plugin.InvalidDescriptionException | RuntimeException e) {
            return null;
        }
    }

    static String sha256(File file) {
        try {
            return hex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(file.toPath())));
        } catch (IOException | NoSuchAlgorithmException e) {
            return "";
        }
    }

    static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder();
        for (byte b : bytes) result.append(String.format("%02x", b));
        return result.toString();
    }

    private JsonObject readCache(String id) {
        File file = new File(cacheDir, id + ".json");
        if (!file.isFile()) return null;
        try (Reader reader = Files.newBufferedReader(file.toPath(), StandardCharsets.UTF_8)) {
            return parseObject(reader);
        } catch (IOException e) {
            return null;
        }
    }

    private void writeCache(Registered registered, boolean active, AddonConfig config) {
        JsonObject cache = new JsonObject();
        cache.addProperty("active", active);
        cache.addProperty("sha256", registered.sha256);  // a replaced jar starts inactive until MCConnect approves it
        cache.add("config", config.toJson());
        try {
            Files.createDirectories(cacheDir.toPath());
            Files.write(new File(cacheDir, registered.id + ".json").toPath(), cache.toString().getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            plugin.getLogger().warning("Could not cache the settings of addon " + registered.id + ": " + e.getMessage());
        }
    }

    // ------------------------------------------------------------------ one addon

    final class Registered implements AddonHandle {
        final JavaPlugin owner;
        final Addon addon;
        final String id;
        final JsonObject manifest;
        final File jar;
        final String sha256;
        private volatile boolean active;
        private volatile AddonConfig config;
        private boolean stateFromServer;
        private List<String> problems = Collections.emptyList();

        Registered(JavaPlugin owner, Addon addon, String id, JsonObject manifest, File jar, String sha256) {
            this.owner = owner;
            this.addon = addon;
            this.id = id;
            this.manifest = manifest;
            this.jar = jar;
            this.sha256 = sha256;
        }

        void apply(boolean nowActive, AddonConfig newConfig) {
            if (!owner.isEnabled()) return;
            if (nowActive) {
                if (active && newConfig.equals(config)) return;
                boolean first = !active;
                active = true;
                config = newConfig;
                if (first) plugin.getLogger().info("Addon " + id + " is active");
                call("onActivate", () -> addon.onActivate(newConfig));
            } else if (active) {
                active = false;
                config = null;
                plugin.getLogger().info("Addon " + id + " is switched off (not approved or paused on the MCConnect website)");
                call("onDeactivate", addon::onDeactivate);
            }
            updateCommands();
        }

        /** An addon's error must not break MCDataLink. */
        void call(String what, Runnable action) {
            try {
                action.run();
            } catch (RuntimeException | LinkageError e) {
                plugin.getLogger().log(Level.WARNING, "Addon " + id + " failed in " + what, e);
            }
        }

        void sendProblems() {
            JsonArray list = new JsonArray();
            for (String problem : problems) list.add(problem);
            plugin.sendToMCConnect("!ADDONPROBLEMS~" + id + "|" + list);
        }

        @Override
        public String id() {
            return id;
        }

        @Override
        public boolean isActive() {
            return active;
        }

        @Override
        public boolean isConnected() {
            return plugin.isConnected();
        }

        @Override
        public AddonConfig config() {
            return config;
        }

        @Override
        public boolean send(String message) {
            return active && plugin.sendToMCConnect("!ADDON~" + id + "|" + message);
        }

        @Override
        public boolean saveConfig(JsonObject newConfig, Player actor) {
            return active && plugin.sendToMCConnect("!ADDONCONFIG~" + id + "|" + (actor == null ? "" : actor.getName()) + "|" + newConfig);
        }

        @Override
        public void reportProblems(List<String> list) {
            List<String> cleaned = new ArrayList<>();
            for (String problem : list) {
                if (cleaned.size() >= MAX_PROBLEMS) break;
                String text = problem.replace('\n', ' ');
                cleaned.add(text.length() > MAX_PROBLEM_LENGTH ? text.substring(0, MAX_PROBLEM_LENGTH) : text);
            }
            if (cleaned.equals(problems)) return;
            problems = cleaned;
            for (String problem : cleaned) plugin.getLogger().warning("Addon " + id + ": " + problem);
            sendProblems();
        }

        @Override
        public boolean command(Player player, String name, String args) {
            return plugin.sendCommand(InGameCommands.commandMessage(player, name, args));
        }

        @Override
        public ChatLine chat(String markup) {
            Glyphs.Line line = plugin.glyphs().line(markup);
            return new ChatLine() {
                @Override
                public void send(Player player) {
                    player.spigot().sendMessage(line.of(player));
                }

                @Override
                public void broadcast() {
                    for (Player player : plugin.getServer().getOnlinePlayers()) send(player);
                }
            };
        }

        @Override
        public String plain(String markup) {
            return ChatMarkup.plain(plugin.glyphs().fallback(markup));
        }
    }
}
