package org.tobias.mcdatalink;

import org.bukkit.ChatColor;
import org.bukkit.Location;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.entity.Player;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * /stats, /top, /wettbewerb, /duell, /report, /seitenleiste, /vote and /events. The commands are answered by
 * MCConnect: the plugin only forwards them with the player's position (!CMD) and shows the
 * answer lines (!tell).
 */
final class InGameCommands implements CommandExecutor, TabCompleter {
    static final String[] COMMANDS = {"stats", "top", "wettbewerb", "duell", "report", "seitenleiste", "vote", "events"};
    private static final List<String> PERIODS = Arrays.asList("7", "30");
    private static final List<String> DAYS = Arrays.asList("1", "2", "3", "4", "5", "6", "7");
    private static final List<String> DUEL_ACTIONS = Arrays.asList("annehmen", "ablehnen");
    private static final List<String> SIDEBAR_MODES = Arrays.asList("aus", "wettbewerb", "spielzeit");
    private static final List<String> NUMBERS = Arrays.asList("1", "2", "3", "4", "5", "6", "7", "8");
    private static final List<String> EVENT_ACTIONS = Arrays.asList("anmelden", "abmelden");

    private final MCDataLink plugin;
    /** Metric names for the tab completion, sent by MCConnect after connecting (!metrics). */
    private volatile List<String> metricNames = Collections.emptyList();

    InGameCommands(MCDataLink plugin) {
        this.plugin = plugin;
    }

    void setMetricNames(List<String> names) {
        metricNames = Collections.unmodifiableList(new ArrayList<>(names));
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player)) {
            sender.sendMessage("Dieser Befehl geht nur im Spiel.");
            return true;
        }
        Player player = (Player) sender;
        Location at = player.getLocation();
        String world = at.getWorld() == null ? "" : clean(at.getWorld().getName());
        String message = "!CMD~" + player.getUniqueId() + "|" + world + "|" + at.getBlockX() + "|" + at.getBlockY() + "|"
                + at.getBlockZ() + "|" + command.getName() + "|" + clean(String.join(" ", args));
        if (!plugin.sendCommand(message)) {
            player.sendMessage(ChatColor.RED + "MCConnect ist gerade nicht erreichbar. Versuche es gleich noch einmal.");
        }
        return true;
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        String name = command.getName();
        int index = args.length - 1;
        List<String> options = Collections.emptyList();
        if (name.equals("stats") && index == 0) {
            options = onlineNames();
        } else if (name.equals("top")) {
            options = index == 0 ? metricNames : index == 1 ? PERIODS : options;
        } else if (name.equals("duell")) {
            if (index == 0) {
                options = new ArrayList<>(DUEL_ACTIONS);
                options.addAll(onlineNames());
            } else if (!DUEL_ACTIONS.contains(args[0].toLowerCase())) {
                options = index == 1 ? metricNames : index == 2 ? DAYS : options;
            }
        } else if (name.equals("report") && index == 0) {
            options = onlineNames();
        } else if (name.equals("seitenleiste") && index == 0) {
            options = SIDEBAR_MODES;
        } else if (name.equals("vote") && index <= 1) {
            options = NUMBERS;
        } else if (name.equals("events")) {
            options = index == 0 ? EVENT_ACTIONS : index == 1 ? NUMBERS : options;
        }
        String prefix = args.length == 0 ? "" : args[index].toLowerCase();
        List<String> matches = new ArrayList<>();
        for (String option : options) {
            if (option.toLowerCase().startsWith(prefix)) matches.add(option);
        }
        return matches;
    }

    private List<String> onlineNames() {
        List<String> names = new ArrayList<>();
        for (Player player : plugin.getServer().getOnlinePlayers()) names.add(player.getName());
        return names;
    }

    /** No protocol separators in free text. */
    private static String clean(String text) {
        return text.replace('|', '/').replace('~', '-').replace('\n', ' ');
    }
}
