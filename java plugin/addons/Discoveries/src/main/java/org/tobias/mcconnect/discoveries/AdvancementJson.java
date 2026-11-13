package org.tobias.mcconnect.discoveries;

import com.google.gson.JsonObject;

/**
 * The advancement definitions (data pack JSON) of the "Entdeckungen" tab. The format differs between Minecraft
 * versions: the icon is {"item": ...} before 1.20.5 and {"id": ...} since; the background of the tab includes
 * "textures/" and ".png" before 1.21.5 and is relative to the textures folder since.
 */
final class AdvancementJson {
    static final String CRITERION = "found";

    private final boolean itemId;
    private final boolean relativeBackground;

    /** version: the Minecraft version, e.g. "1.21.4" or "26.1" */
    AdvancementJson(String version) {
        int[] v = parse(version);
        itemId = compare(v, 1, 20, 5) >= 0;
        relativeBackground = compare(v, 1, 21, 5) >= 0;
    }

    /** "1.21.4-R0.1-SNAPSHOT" -> {1, 21, 4}; missing parts are 0. */
    static int[] parse(String version) {
        int[] parts = new int[3];
        String plain = version == null ? "" : version.split("-", 2)[0];
        String[] pieces = plain.split("\\.");
        for (int i = 0; i < 3 && i < pieces.length; i++) {
            try {
                parts[i] = Integer.parseInt(pieces[i].trim());
            } catch (NumberFormatException e) {
                break;
            }
        }
        return parts;
    }

    private static int compare(int[] v, int major, int minor, int patch) {
        if (v[0] != major) return Integer.compare(v[0], major);
        if (v[1] != minor) return Integer.compare(v[1], minor);
        return Integer.compare(v[2], patch);
    }

    /** The tab: never announced, granted together with the first place a player finds. */
    String root() {
        JsonObject display = display("filled_map", "Entdeckungen", "Orte, die du auf diesem Server gefunden hast", false);
        display.addProperty("background", relativeBackground ? "minecraft:gui/advancements/backgrounds/adventure"
                : "minecraft:textures/gui/advancements/backgrounds/adventure.png");
        return advancement(null, display);
    }

    /** A place: hidden until found, then with toast and chat line. */
    String place(String parent, Area area) {
        JsonObject display = display(area.icon, area.title, area.description, true);
        display.addProperty("hidden", true);
        return advancement(parent, display);
    }

    private JsonObject display(String material, String title, String description, boolean announce) {
        JsonObject icon = new JsonObject();
        icon.addProperty(itemId ? "id" : "item", "minecraft:" + material);
        JsonObject display = new JsonObject();
        display.add("icon", icon);
        display.add("title", text(title));
        display.add("description", text(description));
        display.addProperty("frame", "task");
        display.addProperty("show_toast", announce);
        display.addProperty("announce_to_chat", announce);
        display.addProperty("hidden", false);
        return display;
    }

    private static JsonObject text(String text) {
        JsonObject component = new JsonObject();
        component.addProperty("text", text);
        return component;
    }

    private static String advancement(String parent, JsonObject display) {
        JsonObject trigger = new JsonObject();
        trigger.addProperty("trigger", "minecraft:impossible");  // only the addon grants it
        JsonObject criteria = new JsonObject();
        criteria.add(CRITERION, trigger);
        JsonObject json = new JsonObject();
        if (parent != null) json.addProperty("parent", parent);
        json.add("display", display);
        json.add("criteria", criteria);
        return json.toString();
    }
}
