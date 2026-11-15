package org.tobias.mcconnect.tickets;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.bukkit.ChatColor;
import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommand;
import org.bukkit.entity.Player;
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
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * /ticket add|status|close: tickets of the players for the moderators. MCConnect stores the tickets, answers the
 * player in the chat and shows them to the moderators on the website (page /tickets).
 *
 * A new ticket is sent as "add|request id|uuid|category|world|x|y|z|epoch millis|text" and kept in
 * plugins/Tickets/state.json until MCConnect answers "done|request id", so a ticket written while MCConnect is not
 * reachable is sent later (the request id makes a repeat harmless). Status and close need the connection.
 */
public final class Tickets extends JavaPlugin implements Addon {
    /** the same rule as database/tickets.py */
    static final Pattern CATEGORY = Pattern.compile("^[a-z0-9_-]{1,20}$");
    private static final int MIN_TEXT = 5;
    private static final int MAX_TEXT = 500;
    /** tickets of one player that wait for MCConnect at most (spam while MCConnect is away) */
    private static final int MAX_WAITING = 3;
    private static final long RETRY_TICKS = 20L * 60;

    private AddonHandle handle;
    private volatile List<String> categories = Collections.emptyList();
    /** request id -> the add message, not confirmed by MCConnect yet */
    private final Map<String, String> pending = new LinkedHashMap<>();
    private BukkitTask retryTask;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). Tickets needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        loadState();
        handle = MCConnect.register(this, this);
        PluginCommand command = getCommand("ticket");
        if (command != null) {
            command.setExecutor(this);
            command.setTabCompleter(this);
        }
        retryTask = getServer().getScheduler().runTaskTimer(this, this::report, RETRY_TICKS, RETRY_TICKS);
    }

    @Override
    public void onDisable() {
        if (retryTask != null) retryTask.cancel();
        saveState();
    }

    @Override
    public void onActivate(AddonConfig config) {
        List<String> valid = new ArrayList<>();
        List<String> problems = new ArrayList<>();
        for (String line : config.getStringList("categories")) {
            String category = line.trim().toLowerCase(Locale.ROOT);
            if (category.isEmpty() || valid.contains(category)) continue;
            if (CATEGORY.matcher(category).matches()) {
                valid.add(category);
            } else {
                problems.add("Kategorie »" + line.trim() + "«: nur Buchstaben, Ziffern, _ und -, ohne Leerzeichen (höchstens 20).");
            }
        }
        if (valid.isEmpty()) problems.add("Es gibt keine gültige Kategorie, so kann niemand ein Ticket schreiben.");
        categories = Collections.unmodifiableList(valid);
        handle.reportProblems(problems);
        report();
    }

    @Override
    public void onDeactivate() {
        categories = Collections.emptyList();
    }

    @Override
    public void onMessage(String message) {
        String[] parts = message.split("\\|");
        if (parts.length == 2 && parts[0].equals("done") && pending.remove(parts[1]) != null) saveState();
    }

    /** Sends the tickets MCConnect has not confirmed yet. */
    private void report() {
        if (handle == null || pending.isEmpty() || !handle.isActive() || !handle.isConnected()) return;
        for (String message : pending.values()) {
            if (!handle.send(message)) return;
        }
    }

    // ------------------------------------------------------------------ /ticket

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (handle == null || !handle.isActive()) {
            sender.sendMessage(ChatColor.GRAY + "Tickets sind auf diesem Server gerade ausgeschaltet.");
            return true;
        }
        if (!(sender instanceof Player)) {
            sender.sendMessage(ChatColor.GRAY + "Tickets gibt es nur im Spiel. Die Moderatoren sehen sie auf der MCConnect-Website.");
            return true;
        }
        Player player = (Player) sender;
        String action = args.length == 0 ? "" : args[0].toLowerCase(Locale.ROOT);
        if (action.equals("add")) {
            add(player, args);
        } else if (action.equals("status") && args.length <= 2) {
            if (args.length == 2 && number(args[1]) == null) {
                usage(player);
            } else {
                ask(player, "status|" + player.getUniqueId() + "|" + (args.length == 2 ? number(args[1]) : ""));
            }
        } else if (action.equals("close") && args.length == 2 && number(args[1]) != null) {
            ask(player, "close|" + player.getUniqueId() + "|" + number(args[1]));
        } else {
            usage(player);
        }
        return true;
    }

    private void usage(Player player) {
        player.sendMessage(ChatColor.GOLD + "Tickets " + ChatColor.GRAY + "– ein Anliegen an die Moderatoren");
        player.sendMessage(ChatColor.GRAY + "/ticket add <kategorie> <text> " + ChatColor.DARK_GRAY + "– neues Ticket, deine Position wird mitgeschickt");
        player.sendMessage(ChatColor.GRAY + "/ticket status [nummer] " + ChatColor.DARK_GRAY + "– deine Tickets und die Antworten");
        player.sendMessage(ChatColor.GRAY + "/ticket close <nummer> " + ChatColor.DARK_GRAY + "– ein eigenes Ticket schließen");
        if (!categories.isEmpty()) player.sendMessage(ChatColor.GRAY + "Kategorien: " + ChatColor.WHITE + String.join(", ", categories));
    }

    private void add(Player player, String[] args) {
        if (args.length < 3) {
            usage(player);
            return;
        }
        String category = args[1].toLowerCase(Locale.ROOT);
        if (!categories.contains(category)) {
            player.sendMessage(ChatColor.RED + "Die Kategorie »" + args[1] + "« gibt es nicht. " + ChatColor.GRAY
                    + "Kategorien: " + ChatColor.WHITE + String.join(", ", categories));
            return;
        }
        String text = String.join(" ", Arrays.copyOfRange(args, 2, args.length)).trim();
        if (text.length() < MIN_TEXT || text.length() > MAX_TEXT) {
            player.sendMessage(ChatColor.RED + "Beschreibe dein Anliegen in " + MIN_TEXT + " bis " + MAX_TEXT + " Zeichen.");
            return;
        }
        String uuid = player.getUniqueId().toString();
        int waiting = 0;
        for (String message : pending.values()) if (message.contains("|" + uuid + "|")) waiting++;
        if (waiting >= MAX_WAITING) {
            player.sendMessage(ChatColor.RED + "Deine letzten Tickets warten noch darauf, gesendet zu werden. Versuche es später noch einmal.");
            return;
        }
        Location at = player.getLocation();
        String request = UUID.randomUUID().toString().replace("-", "");
        String message = "add|" + request + "|" + uuid + "|" + category + "|" + at.getWorld().getName() + "|" + at.getBlockX()
                + "|" + at.getBlockY() + "|" + at.getBlockZ() + "|" + System.currentTimeMillis() + "|" + text;
        pending.put(request, message);
        saveState();
        if (handle.isConnected() && handle.send(message)) {
            player.sendMessage(ChatColor.GRAY + "Ticket wird gesendet …");
        } else {
            player.sendMessage(ChatColor.YELLOW + "MCConnect ist gerade nicht erreichbar. Dein Ticket wird gesendet, sobald die Verbindung wieder steht.");
        }
    }

    /** status and close: MCConnect answers in the chat. */
    private void ask(Player player, String message) {
        if (!handle.isConnected() || !handle.send(message)) {
            player.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar. Versuche es gleich noch einmal.");
        }
    }

    /** A ticket number (1 or more), or null. */
    private static Integer number(String text) {
        try {
            int value = Integer.parseInt(text.startsWith("#") ? text.substring(1) : text);
            return value >= 1 ? Integer.valueOf(value) : null;
        } catch (NumberFormatException e) {
            return null;
        }
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> matches = new ArrayList<>();
        if (args.length == 1) {
            for (String option : Arrays.asList("add", "status", "close")) {
                if (option.startsWith(args[0].toLowerCase(Locale.ROOT))) matches.add(option);
            }
        } else if (args.length == 2 && args[0].equalsIgnoreCase("add")) {
            for (String category : categories) if (category.startsWith(args[1].toLowerCase(Locale.ROOT))) matches.add(category);
        }
        return matches;
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
            if (state.has("pending")) {
                for (Map.Entry<String, JsonElement> entry : state.getAsJsonObject("pending").entrySet()) {
                    pending.put(entry.getKey(), entry.getValue().getAsString());
                }
            }
        } catch (Exception e) {
            getLogger().warning("Could not read " + file + ": " + e);
        }
    }

    private void saveState() {
        JsonObject open = new JsonObject();
        for (Map.Entry<String, String> entry : pending.entrySet()) open.addProperty(entry.getKey(), entry.getValue());
        JsonObject state = new JsonObject();
        state.add("pending", open);
        try {
            File file = stateFile();
            if (!file.getParentFile().isDirectory() && !file.getParentFile().mkdirs()) throw new IOException("no data folder");
            File temp = new File(file.getParentFile(), "state.json.tmp");
            Files.write(temp.toPath(), state.toString().getBytes(StandardCharsets.UTF_8));
            Files.move(temp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            getLogger().warning("Could not save the state: " + e);
        }
    }
}
