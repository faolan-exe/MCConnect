package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.EOFException;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Keeps a connection to the MCConnect socket server and reports joins, quits and
 * player statistics. All network I/O happens off the main server thread.
 *
 * Wire format: 10 byte space padded length header + UTF-8 payload (see mc_socket/main.py).
 */
public final class MCDataLink extends JavaPlugin {

    private static final int HEADER = 10;
    private static final int MAX_MESSAGE_SIZE = 16 * 1024 * 1024;
    private static final int CONNECT_TIMEOUT_MS = 5000;
    /** The server sends a heartbeat every 5 seconds; silence for this long means the connection is dead. */
    private static final int READ_TIMEOUT_MS = 20000;
    private static final int HEARTBEAT_INTERVAL_SECONDS = 5;
    private static final int MAX_RETRY_SECONDS = 60;
    /** Delay before sending the stats of a player who left, so the server has saved them. */
    private static final long QUIT_STATS_DELAY_TICKS = 40L;
    /** How often the server's ban list is reported to MCConnect (60 seconds). */
    private static final long BAN_SYNC_INTERVAL_TICKS = 20L * 60;

    private final Object sendLock = new Object();
    private volatile Socket socket;
    private volatile OutputStream out;
    private volatile boolean authenticated;
    private volatile boolean running;

    private Thread connectionThread;
    private ScheduledExecutorService worker;
    private String key;
    private String host;
    private int port;
    private File worldFolder;
    /** Found lazily: the folder only exists once the game has saved stats. */
    private volatile File statsDir;
    private volatile boolean missingStatsLogged;
    private PrefixDisplay prefixDisplay;
    private BanSync banSync;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        key = getConfig().getString("key", "").trim();
        host = getConfig().getString("host", "mc.tobisit.de").trim();
        port = getConfig().getInt("port", 9991);
        if (key.isEmpty() || key.contains("<")) {
            getLogger().severe("No server key configured. Enter your key in plugins/MCDataLink/config.yml and restart.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        prefixDisplay = new PrefixDisplay(this, getConfig().getBoolean("prefix.chat", true),
                getConfig().getBoolean("prefix.tablist", true), getConfig().getBoolean("prefix.nametag", true));
        banSync = new BanSync(this);
        getServer().getScheduler().runTaskTimer(this, this::sendBanList, BAN_SYNC_INTERVAL_TICKS, BAN_SYNC_INTERVAL_TICKS);

        List<World> worlds = getServer().getWorlds();
        worldFolder = worlds.isEmpty() ? new File(getServer().getWorldContainer(), "world") : worlds.get(0).getWorldFolder();

        worker = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "MCDataLink-worker");
            t.setDaemon(true);
            return t;
        });
        worker.scheduleAtFixedRate(() -> {
            if (authenticated) trySend("!BEAT");
        }, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS, TimeUnit.SECONDS);

        running = true;
        connectionThread = new Thread(this::connectionLoop, "MCDataLink-connection");
        connectionThread.setDaemon(true);
        connectionThread.start();

        getServer().getPluginManager().registerEvents(new JoinListener(this), this);
        getLogger().info("MCDataLink enabled, connecting to " + host + ":" + port);
    }

    @Override
    public void onDisable() {
        running = false;
        if (authenticated) trySend("!DISCONNECT");
        closeSocket();
        if (worker != null) worker.shutdownNow();
        if (connectionThread != null) {
            connectionThread.interrupt();
            try {
                connectionThread.join(2000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }

    // ------------------------------------------------------------------ connection

    private void connectionLoop() {
        int retrySeconds = 1;
        while (running) {
            try {
                connectAndServe();
            } catch (AuthenticationException e) {
                getLogger().severe(e.getMessage());
                running = false;
                closeSocket();
                runOnMainThread(() -> getServer().getPluginManager().disablePlugin(this));
                return;
            } catch (IOException e) {
                if (running) getLogger().warning("Connection to MCConnect (" + host + ":" + port + ") lost: " + e.getMessage());
            } finally {
                closeSocket();
            }
            if (!running) return;
            if (authenticated) retrySeconds = 1;
            authenticated = false;
            try {
                TimeUnit.SECONDS.sleep(retrySeconds);
            } catch (InterruptedException e) {
                return;
            }
            retrySeconds = Math.min(retrySeconds * 2, MAX_RETRY_SECONDS);
        }
    }

    private void connectAndServe() throws IOException, AuthenticationException {
        Socket s = new Socket();
        s.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
        s.setSoTimeout(READ_TIMEOUT_MS);
        s.setKeepAlive(true);
        InputStream in = new BufferedInputStream(s.getInputStream());
        synchronized (sendLock) {
            socket = s;
            out = new BufferedOutputStream(s.getOutputStream());
        }
        send("!AUTH~" + key);
        while (running) {
            handleMessage(readMessage(in));
        }
    }

    private void handleMessage(String msg) throws IOException, AuthenticationException {
        if (msg.equals("!heartbeat")) return;

        if (msg.startsWith("success|") || msg.startsWith("error|")) {
            String code = msg.substring(msg.indexOf('|') + 1);
            switch (code) {
                case "100":
                    authenticated = true;
                    getLogger().info("Connected to MCConnect");
                    sendOnlinePlayers();
                    runOnMainThread(this::sendBanList);
                    break;
                case "001":
                case "002":
                    throw new AuthenticationException("MCConnect rejected the server key. Check plugins/MCDataLink/config.yml and restart.");
                case "000":
                    throw new IOException("closed by server");
                default:
                    if (msg.startsWith("error|")) getLogger().fine("MCConnect answered " + msg);
            }
            return;
        }

        int separator = msg.indexOf('~');
        String command = separator < 0 ? msg : msg.substring(0, separator);
        String value = separator < 0 ? "" : msg.substring(separator + 1);
        String[] fields = value.split("\\|", -1);
        switch (command) {
            case "!sendAllPlayerStats":
                worker.execute(this::sendAllPlayerStats);
                break;
            case "!sendPlayerStats": {
                UUID uuid = parseUuid(value);
                if (uuid != null) worker.execute(() -> sendPlayerStats(uuid));
                break;
            }
            case "!loginPin": {
                String[] parts = value.split("~");
                UUID uuid = parts.length > 1 ? parseUuid(parts[0]) : null;
                if (uuid != null) runOnMainThread(() -> showLoginPin(uuid, parts[1]));
                break;
            }
            case "!prefix": {  // uuid|color|text
                UUID uuid = fields.length >= 3 ? parseUuid(fields[0]) : null;
                if (uuid != null) prefixDisplay.set(uuid, fields[1], fields[2]);
                break;
            }
            case "!ban": {  // uuid|name|end millis (0 = permanent)|reason
                UUID uuid = fields.length >= 4 ? parseUuid(fields[0]) : null;
                if (uuid != null) {
                    long end = parseLong(fields[2]);
                    runOnMainThread(() -> banSync.ban(uuid, fields[1], end, fields[3]));
                }
                break;
            }
            case "!unban":  // uuid|name
                if (fields.length >= 2) runOnMainThread(() -> banSync.unban(fields[1]));
                break;
            default:
                getLogger().fine("Unknown message from MCConnect: " + msg);
        }
    }

    private String readMessage(InputStream in) throws IOException {
        String header = new String(readExactly(in, HEADER), StandardCharsets.UTF_8).trim();
        int length;
        try {
            length = Integer.parseInt(header);
        } catch (NumberFormatException e) {
            throw new IOException("invalid message header '" + header + "'");
        }
        if (length < 0 || length > MAX_MESSAGE_SIZE) throw new IOException("invalid message length " + length);
        return new String(readExactly(in, length), StandardCharsets.UTF_8);
    }

    private static byte[] readExactly(InputStream in, int length) throws IOException {
        byte[] buf = new byte[length];
        int read = 0;
        while (read < length) {
            int r = in.read(buf, read, length - read);
            if (r == -1) throw new EOFException("connection closed");
            read += r;
        }
        return buf;
    }

    private void send(String msg) throws IOException {
        byte[] payload = msg.getBytes(StandardCharsets.UTF_8);
        byte[] header = String.format("%-" + HEADER + "s", payload.length).getBytes(StandardCharsets.UTF_8);
        synchronized (sendLock) {
            if (out == null) throw new IOException("not connected");
            out.write(header);
            out.write(payload);
            out.flush();
        }
    }

    /** Send if connected; failures are only logged, the connection thread handles reconnects. */
    private boolean trySend(String msg) {
        try {
            send(msg);
            return true;
        } catch (IOException e) {
            getLogger().fine("Could not send to MCConnect: " + e.getMessage());
            return false;
        }
    }

    private void sendAsync(String msg) {
        if (worker == null || worker.isShutdown()) return;
        worker.execute(() -> {
            if (authenticated) trySend(msg);
        });
    }

    private void closeSocket() {
        synchronized (sendLock) {
            try {
                if (socket != null) socket.close();
            } catch (IOException ignored) {
            }
            socket = null;
            out = null;
        }
    }

    // ------------------------------------------------------------------ players & stats

    void playerJoined(Player player) {
        sendAsync("!JOIN~" + player.getUniqueId() + "|" + player.getName() + "|" + (player.isOp() ? "1" : "0"));
    }

    PrefixDisplay prefixDisplay() {
        return prefixDisplay;
    }

    /** Main thread: report the server's own bans (not MCConnect's) to the website. */
    private void sendBanList() {
        if (!authenticated) return;
        String json = banSync.banListJson();
        if (json != null) sendAsync("!BANS~" + json);
    }

    void playerQuit(Player player) {
        UUID uuid = player.getUniqueId();
        prefixDisplay.clear(player);
        sendAsync("!QUIT~" + uuid);
        getServer().getScheduler().runTaskLaterAsynchronously(this, () -> sendPlayerStats(uuid), QUIT_STATS_DELAY_TICKS);
    }

    /** Called on the main thread after the main world was saved (stats files are up to date then). */
    void mainWorldSaved() {
        List<UUID> online = new ArrayList<>();
        for (Player player : getServer().getOnlinePlayers()) online.add(player.getUniqueId());
        worker.execute(() -> online.forEach(this::sendPlayerStats));
    }

    boolean isMainWorld(World world) {
        return !getServer().getWorlds().isEmpty() && getServer().getWorlds().get(0).equals(world);
    }

    /** After (re)connecting the server marks everyone offline, so report who is online. */
    private void sendOnlinePlayers() {
        runOnMainThread(() -> {
            for (Player player : getServer().getOnlinePlayers()) playerJoined(player);
        });
    }

    private void sendAllPlayerStats() {
        File dir = statsDir();
        File[] files = dir == null ? null : dir.listFiles((d, name) -> name.endsWith(".json"));
        if (files == null) return;
        int sent = 0;
        for (File file : files) {
            UUID uuid = parseUuid(file.getName().substring(0, file.getName().length() - ".json".length()));
            if (uuid != null && sendPlayerStats(uuid)) sent++;
        }
        getLogger().info("Sent stats of " + sent + " players to MCConnect");
    }

    private boolean sendPlayerStats(UUID uuid) {
        if (!authenticated) return false;
        File dir = statsDir();
        if (dir == null) return false;
        File file = new File(dir, uuid + ".json");
        if (!file.isFile()) return false;
        try {
            String json = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
            return trySend("!STATS~" + uuid + "|" + json);
        } catch (IOException e) {
            getLogger().warning("Could not read stats of " + uuid + ": " + e.getMessage());
            return false;
        }
    }

    private void showLoginPin(UUID uuid, String pin) {
        Player player = getServer().getPlayer(uuid);
        if (player == null) return;
        player.sendMessage(ChatColor.GOLD + "[MCConnect] " + ChatColor.GRAY + "Dein Login-PIN: "
                + ChatColor.GREEN + ChatColor.BOLD + pin);
        player.sendMessage(ChatColor.GRAY + "Gültig für 5 Minuten. Gib ihn niemals weiter!");
    }

    // ------------------------------------------------------------------ helpers

    /** The folder with the players' stats files, or null if the game has not written any yet. */
    private File statsDir() {
        File cached = statsDir;
        if (cached != null && cached.isDirectory()) return cached;
        File found = StatsLocator.find(worldFolder, getServer().getWorldContainer());
        if (found != null) {
            getLogger().info("Using player stats from " + found);
            statsDir = found;
        } else if (!missingStatsLogged) {
            missingStatsLogged = true;
            getLogger().info("No player stats in " + worldFolder + " yet. Minecraft writes them when the world is saved;"
                    + " they will be sent then.");
        }
        return found;
    }

    void runOnMainThread(Runnable task) {
        if (isEnabled()) getServer().getScheduler().runTask(this, task);
    }

    private static long parseLong(String value) {
        try {
            return Long.parseLong(value.trim());
        } catch (NumberFormatException e) {
            return 0L;
        }
    }

    private static UUID parseUuid(String value) {
        try {
            return UUID.fromString(value.trim());
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static final class AuthenticationException extends Exception {
        AuthenticationException(String message) {
            super(message);
        }
    }
}
