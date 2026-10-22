package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Automatic updates (!update, on by default, "auto-update: false" switches it off). MCConnect sends the version and
 * SHA-256 of its plugin jar over the pinned TLS connection; a newer version is downloaded, installed only if it has
 * exactly that checksum, and put into the server's update folder (plugins/update/, bukkit.yml settings.update-folder)
 * under the name of the running jar. Bukkit and Paper replace the jar from there on the next start; nothing changes
 * while the server runs. OPs are told in the chat.
 */
final class PluginUpdater implements Listener {
    private static final int MAX_SIZE = 32 * 1024 * 1024;
    private static final int TIMEOUT_MS = 20000;
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");

    private final MCDataLink plugin;
    private final File ownJar;
    private final boolean enabled;
    /** The version waiting in the update folder, or null. Set by the worker thread. */
    private volatile String installed;
    /** The version currently being downloaded or found broken, so it is not tried again and again. */
    private volatile String tried;

    PluginUpdater(MCDataLink plugin, File ownJar, boolean enabled) {
        this.plugin = plugin;
        this.ownJar = ownJar;
        this.enabled = enabled;
    }

    /** From the connection thread: !update version|url|sha256. */
    void offer(String version, String url, String sha256) {
        String current = plugin.getDescription().getVersion();
        if (compare(version, current) <= 0 || version.equals(installed) || version.equals(tried)) return;
        if (!enabled) {
            if (!version.equals(tried)) {
                tried = version;
                plugin.getLogger().info("MCDataLink " + version + " is available (running " + current
                        + "). Automatic updates are off (auto-update in config.yml); download it on the MCConnect admin page.");
            }
            return;
        }
        if (!SHA256.matcher(sha256.toLowerCase()).matches() || !(url.startsWith("https://") || url.startsWith("http://"))) {
            plugin.getLogger().warning("MCConnect sent an invalid update (" + version + "), ignored.");
            return;
        }
        tried = version;
        try {
            byte[] jar = download(url);
            String actual = hex(MessageDigest.getInstance("SHA-256").digest(jar));
            if (!actual.equals(sha256.toLowerCase())) {
                plugin.getLogger().warning("Update " + version + " not installed: the download has another checksum than"
                        + " MCConnect announced.");
                return;
            }
            File folder = plugin.getServer().getUpdateFolderFile();
            if (!folder.isDirectory() && !folder.mkdirs()) throw new IOException("cannot create " + folder);
            File temp = new File(folder, ownJar.getName() + ".part");
            Files.write(temp.toPath(), jar);
            Files.move(temp.toPath(), new File(folder, ownJar.getName()).toPath(), StandardCopyOption.REPLACE_EXISTING);
            installed = version;
            plugin.getLogger().info("MCDataLink " + version + " downloaded; it is active after the next server restart.");
            plugin.runOnMainThread(() -> {
                for (Player player : plugin.getServer().getOnlinePlayers()) tell(player);
            });
        } catch (IOException | NoSuchAlgorithmException | RuntimeException e) {
            plugin.getLogger().warning("Could not download the update " + version + ": " + e);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        tell(event.getPlayer());
    }

    private void tell(Player player) {
        String version = installed;
        if (version != null && player.isOp()) {
            player.sendMessage(ChatColor.GOLD + "[MCConnect] " + ChatColor.GRAY + "MCDataLink " + version
                    + " ist bereit und wird beim nächsten Server-Neustart aktiv.");
        }
    }

    static byte[] download(String url) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(TIMEOUT_MS);
        connection.setReadTimeout(TIMEOUT_MS);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestProperty("User-Agent", "MCDataLink");
        try {
            if (connection.getResponseCode() != 200) throw new IOException("HTTP " + connection.getResponseCode());
            try (InputStream in = connection.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buffer = new byte[16384];
                int read;
                while ((read = in.read(buffer)) != -1) {
                    if (out.size() + read > MAX_SIZE) throw new IOException("download too large");
                    out.write(buffer, 0, read);
                }
                return out.toByteArray();
            }
        } finally {
            connection.disconnect();
        }
    }

    /** Numeric comparison of versions like "3.9" and "3.17". */
    static int compare(String a, String b) {
        Matcher left = Pattern.compile("\\d+").matcher(a);
        Matcher right = Pattern.compile("\\d+").matcher(b);
        while (true) {
            boolean hasLeft = left.find();
            boolean hasRight = right.find();
            if (!hasLeft && !hasRight) return 0;
            long x = hasLeft ? Long.parseLong(left.group()) : 0;
            long y = hasRight ? Long.parseLong(right.group()) : 0;
            if (x != y) return x < y ? -1 : 1;
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder();
        for (byte b : bytes) result.append(String.format("%02x", b));
        return result.toString();
    }
}
