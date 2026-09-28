package org.tobias.mcdatalink;

import net.md_5.bungee.api.chat.BaseComponent;
import net.md_5.bungee.api.chat.ClickEvent;
import net.md_5.bungee.api.chat.HoverEvent;
import net.md_5.bungee.api.chat.TextComponent;
import org.bukkit.ChatColor;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Chat lines from MCConnect with "&" color codes, buttons written as ⟦label⇒/command⟧ (clicking runs the
 * command as the player) and links (clicking opens them). MCConnect removes the button markers from
 * everything players type, so a button always comes from MCConnect itself. As a second line of defense a
 * button may only run MCDataLink's own commands; any other button is shown as plain text.
 */
final class ChatMarkup {
    private static final Pattern PART = Pattern.compile("⟦([^⇒⟧]*)⇒([^⟧]*)⟧|(https?://[^\\s⟦]+)");
    private static final Pattern COLOR = Pattern.compile("§[0-9a-fk-or]", Pattern.CASE_INSENSITIVE);

    private ChatMarkup() {
    }

    static boolean hasMarkup(String text) {
        return text.indexOf('⟦') >= 0 || text.contains("http://") || text.contains("https://");
    }

    /** The line as chat components. */
    static BaseComponent[] parse(String text) {
        String line = ChatColor.translateAlternateColorCodes('&', text);
        List<BaseComponent> parts = new ArrayList<>();
        String color = "";  // color codes in effect, carried into the next part
        Matcher matcher = PART.matcher(line);
        int position = 0;
        while (matcher.find()) {
            String before = line.substring(position, matcher.start());
            add(parts, color + before, null, null);
            color = lastColors(color + before);
            if (matcher.group(1) != null && !isOwnCommand(matcher.group(2).trim())) {
                add(parts, color + matcher.group(1), null, null);
            } else if (matcher.group(1) != null) {
                String command = matcher.group(2).trim();
                add(parts, color + matcher.group(1), new ClickEvent(ClickEvent.Action.RUN_COMMAND, command),
                        ChatColor.GRAY + "Klicken: " + ChatColor.WHITE + command);
            } else {
                String url = matcher.group(3);
                add(parts, color + url, new ClickEvent(ClickEvent.Action.OPEN_URL, url), ChatColor.GRAY + "Link öffnen");
            }
            position = matcher.end();
        }
        add(parts, color + line.substring(position), null, null);
        return parts.toArray(new BaseComponent[0]);
    }

    /** True for "/<one of InGameCommands.COMMANDS> ...": the only commands a button may run. */
    static boolean isOwnCommand(String command) {
        if (!command.startsWith("/")) return false;
        String name = command.substring(1).split(" ", 2)[0].toLowerCase(Locale.ROOT);
        return Arrays.asList(InGameCommands.COMMANDS).contains(name);
    }

    /** The line without buttons and colors, e.g. for the console. */
    static String plain(String text) {
        return ChatColor.stripColor(ChatColor.translateAlternateColorCodes('&',
                text.replaceAll("⟦([^⇒⟧]*)⇒([^⟧]*)⟧", "$1")));
    }

    @SuppressWarnings("deprecation")  // HoverEvent(Action, BaseComponent[]) is the only constructor in the 1.13 API
    private static void add(List<BaseComponent> parts, String legacy, ClickEvent click, String hover) {
        if (ChatColor.stripColor(legacy).isEmpty()) return;
        for (BaseComponent part : TextComponent.fromLegacyText(legacy)) {
            if (click != null) {
                part.setClickEvent(click);
                part.setHoverEvent(new HoverEvent(HoverEvent.Action.SHOW_TEXT, TextComponent.fromLegacyText(hover)));
            }
            parts.add(part);
        }
    }

    /** The color codes that are in effect at the end of a text (the last color and the formats after it). */
    private static String lastColors(String text) {
        StringBuilder result = new StringBuilder();
        Matcher matcher = COLOR.matcher(text);
        while (matcher.find()) {
            char code = Character.toLowerCase(matcher.group().charAt(1));
            boolean format = code >= 'k' && code <= 'o';
            if (!format) result.setLength(0);  // a color or reset ends all formats
            if (code != 'r') result.append(matcher.group());
        }
        return result.toString();
    }
}
