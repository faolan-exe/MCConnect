package org.tobias.mcdatalink;

import com.google.gson.JsonObject;
import org.bukkit.entity.Player;
import org.bukkit.permissions.Permission;
import org.bukkit.plugin.InvalidDescriptionException;
import org.bukkit.plugin.Plugin;
import org.bukkit.plugin.PluginDescriptionFile;
import org.bukkit.plugin.PluginManager;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * Installs and removes addons from the MCConnect store (!addoninstall, !addonremove; plugin 3.22, "addon-install"
 * in config.yml switches it off). Like PluginUpdater: the jar is only used if it has exactly the SHA-256 that MCConnect
 * sent over the pinned connection, and it must be the addon MCConnect named (its mcconnect-addon.json has that id).
 *
 * A new addon goes into the plugins folder and is loaded and enabled right away. An addon that is already running is
 * updated through the server's update folder (active after the next restart). Removing disables the addon's plugin and
 * deletes its jar; only jars of an addon with that id are ever deleted. The result goes back as !ADDONRESULT.
 */
final class AddonInstaller {
    private static final Pattern FILE_NAME = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\\.jar");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");

    private final MCDataLink plugin;
    private final AddonManager addons;
    private final boolean allowed;
    /** addons being installed right now: a repeated order (e.g. sent again after a reconnect) is ignored */
    private final java.util.Set<String> running = java.util.concurrent.ConcurrentHashMap.newKeySet();

    AddonInstaller(MCDataLink plugin, AddonManager addons, boolean allowed) {
        this.plugin = plugin;
        this.addons = addons;
        this.allowed = allowed;
    }

    /** From the connection thread: !addoninstall~id|version|url|sha256|file name. */
    void install(String value) {
        String[] fields = value.split("\\|", -1);
        if (fields.length < 5) return;
        String id = fields[0];
        if (!running.add(id)) return;
        Thread download = new Thread(() -> {
            try {
                download(id, fields[1], fields[2], fields[3].toLowerCase(Locale.ROOT), fields[4]);
            } finally {
                plugin.runOnMainThread(() -> running.remove(id));  // after place() ran on the main thread
            }
        }, "MCDataLink-addon-install");
        download.setDaemon(true);
        download.start();
    }

    private void download(String id, String version, String url, String sha256, String fileName) {
        if (!allowed) {
            result(id, "failed", "Installieren aus MCConnect ist in plugins/MCDataLink/config.yml ausgeschaltet (addon-install).");
            return;
        }
        if (!AddonManager.ID.matcher(id).matches() || !SHA256.matcher(sha256).matches() || !FILE_NAME.matcher(fileName).matches()
                || !(url.startsWith("https://") || url.startsWith("http://"))) {
            result(id, "failed", "Ungültiger Auftrag von MCConnect.");
            return;
        }
        byte[] jar;
        try {
            jar = PluginUpdater.download(url);
            if (!AddonManager.hex(MessageDigest.getInstance("SHA-256").digest(jar)).equals(sha256)) {
                result(id, "failed", "Die heruntergeladene Datei hat eine andere Prüfsumme als angekündigt.");
                return;
            }
        } catch (IOException | NoSuchAlgorithmException | RuntimeException e) {
            result(id, "failed", "Download fehlgeschlagen: " + e.getMessage());
            return;
        }
        Contents contents = Contents.of(jar);
        if (contents == null || !id.equals(contents.addonId)) {
            result(id, "failed", "Die Datei ist nicht die Erweiterung " + id + ".");
            return;
        }
        plugin.runOnMainThread(() -> place(id, version, fileName, jar, sha256, contents.description));
    }

    /** Main thread: an update into the update folder, a new addon into the plugins folder and load it. */
    private void place(String id, String version, String fileName, byte[] jar, String sha256, PluginDescriptionFile description) {
        PluginManager manager = plugin.getServer().getPluginManager();
        AddonManager.Registered running = addons.byId(id);
        Plugin sameName = pluginNamed(description.getName());
        if (running != null && running.sha256.equals(sha256)) {
            result(id, "installed", version);  // exactly this jar runs already (a repeated order)
            return;
        }
        try {
            if (running != null || sameName != null) {
                if (running == null && !isAddon(sameName, id)) {
                    result(id, "failed", "Auf dem Server läuft schon ein anderes Plugin namens " + sameName.getName()
                            + " (eine alte Version?). Entferne es aus dem plugins-Ordner, starte neu und installiere dann noch einmal.");
                    return;
                }
                File current = running != null ? running.jar : AddonManager.jarOf(sameName, pluginsFolder());
                if (current == null) throw new IOException("the running jar is unknown");
                write(new File(plugin.getServer().getUpdateFolderFile(), current.getName()), jar);
                plugin.getLogger().info("Addon " + id + " " + version + " downloaded; it is active after the next server restart.");
                result(id, "restart", version);
                return;
            }
            File target = new File(pluginsFolder(), fileName);
            if (target.exists() && !id.equals(addonIdOf(target))) target = new File(pluginsFolder(), "MCC-" + fileName);
            write(target, jar);
            Plugin loaded;
            try {
                loaded = manager.loadPlugin(target);
                if (loaded == null) throw new IOException("the server did not load it");
                for (Permission permission : loaded.getDescription().getPermissions()) {
                    try {
                        manager.addPermission(permission);
                    } catch (IllegalArgumentException ignored) {
                        // already known
                    }
                }
                manager.enablePlugin(loaded);
            } catch (Exception | LinkageError e) {
                Files.deleteIfExists(target.toPath());
                result(id, "failed", "Der Server konnte die Erweiterung nicht laden: " + e.getMessage()
                        + " – nach einem Neustart klappt es meist.");
                return;
            }
            if (!loaded.isEnabled()) {
                result(id, "failed", "Die Erweiterung hat sich nicht eingeschaltet, Details im Server-Log.");
                return;
            }
            for (Player player : plugin.getServer().getOnlinePlayers()) updateCommands(player);
            plugin.getLogger().info("Addon " + id + " " + version + " installed and enabled (" + target.getName() + ")");
            result(id, "installed", version);
        } catch (IOException e) {
            result(id, "failed", "Speichern fehlgeschlagen: " + e.getMessage());
        }
    }

    /** From the connection thread: !addonremove~id|file name of the jar (if MCConnect knows it). */
    void remove(String value) {
        String[] fields = value.split("\\|", -1);
        String id = fields[0];
        String fileName = fields.length > 1 ? fields[1] : "";
        plugin.runOnMainThread(() -> {
            AddonManager.Registered running = addons.byId(id);
            File jar = running != null ? running.jar
                    : FILE_NAME.matcher(fileName).matches() ? new File(pluginsFolder(), fileName) : null;
            if (running != null) plugin.getServer().getPluginManager().disablePlugin(running.owner);
            boolean deleted = false;
            if (jar != null && jar.isFile() && id.equals(addonIdOf(jar)) && sameFolder(pluginsFolder(), jar.getParentFile())) {
                deleted = delete(jar);
                File waiting = new File(plugin.getServer().getUpdateFolderFile(), jar.getName());
                if (waiting.isFile() && id.equals(addonIdOf(waiting))) delete(waiting);
            }
            if (running == null && !deleted) {
                result(id, "removed", "");  // nothing (left) on the server
                return;
            }
            plugin.getLogger().info("Addon " + id + " removed" + (deleted ? "" : " (the jar could not be deleted)"));
            result(id, "removed", deleted ? "" : "Die Datei konnte nicht gelöscht werden; sie verschwindet beim nächsten Neustart.");
        });
    }

    private boolean delete(File file) {
        try {
            Files.delete(file.toPath());
            return true;
        } catch (IOException e) {
            file.deleteOnExit();  // e.g. locked on Windows while loaded
            return false;
        }
    }

    private void result(String id, String state, String text) {
        plugin.sendToMCConnect("!ADDONRESULT~" + id + "|" + state + "|" + text.replace('\n', ' '));
        if ("failed".equals(state)) plugin.getLogger().warning("Addon " + id + ": " + text);
    }

    private File pluginsFolder() {
        return plugin.getDataFolder().getParentFile();
    }

    private static boolean sameFolder(File a, File b) {
        return b != null && a.getAbsoluteFile().toPath().normalize().equals(b.getAbsoluteFile().toPath().normalize());
    }

    private Plugin pluginNamed(String name) {
        for (Plugin other : plugin.getServer().getPluginManager().getPlugins()) {
            if (other.getName().equalsIgnoreCase(name)) return other;  // Paper compares names without case
        }
        return null;
    }

    private static boolean isAddon(Plugin other, String id) {
        File jar = AddonManager.jarOf(other, other.getDataFolder().getParentFile());
        return jar != null && id.equals(addonIdOf(jar));
    }

    private static void write(File target, byte[] jar) throws IOException {
        File folder = target.getParentFile();
        if (!folder.isDirectory() && !folder.mkdirs()) throw new IOException("cannot create " + folder);
        File temp = new File(folder, target.getName() + ".part");
        Files.write(temp.toPath(), jar);
        Files.move(temp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING);
    }

    private static String addonIdOf(File jar) {
        try {
            Contents contents = Contents.of(Files.readAllBytes(jar.toPath()));
            return contents == null ? null : contents.addonId;
        } catch (IOException e) {
            return null;
        }
    }

    private static void updateCommands(Player player) {
        try {
            Player.class.getMethod("updateCommands").invoke(player);  // the new commands in the client's tab completion
        } catch (ReflectiveOperationException | LinkageError ignored) {
            // older servers: known after the next join
        }
    }

    /** plugin.yml and the addon id from mcconnect-addon.json of a jar, or null if it has no valid ones. */
    private static final class Contents {
        final PluginDescriptionFile description;
        final String addonId;

        private Contents(PluginDescriptionFile description, String addonId) {
            this.description = description;
            this.addonId = addonId;
        }

        static Contents of(byte[] jar) {
            PluginDescriptionFile description = null;
            String addonId = null;
            try (ZipInputStream zip = new ZipInputStream(new ByteArrayInputStream(jar))) {
                ZipEntry entry;
                while ((entry = zip.getNextEntry()) != null) {
                    if (entry.getName().equals("plugin.yml")) {
                        description = new PluginDescriptionFile(new ByteArrayInputStream(readAll(zip)));
                    } else if (entry.getName().equals("mcconnect-addon.json")) {
                        JsonObject manifest = AddonManager.parseObject(
                                new InputStreamReader(new ByteArrayInputStream(readAll(zip)), StandardCharsets.UTF_8));
                        if (manifest != null && manifest.has("id")) addonId = manifest.get("id").getAsString();
                    }
                }
            } catch (IOException | InvalidDescriptionException | RuntimeException e) {
                return null;
            }
            return description == null || addonId == null ? null : new Contents(description, addonId);
        }

        private static byte[] readAll(ZipInputStream zip) throws IOException {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int read;
            while ((read = zip.read(buffer)) != -1) out.write(buffer, 0, read);
            return out.toByteArray();
        }
    }
}
