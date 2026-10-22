package org.tobias.mcdatalink;

import org.bukkit.Keyed;
import org.bukkit.Material;

import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;

/**
 * The blocks and items of the running server for MCConnect (!REGISTRY, sent after auth and before the stats).
 * MCConnect sorts the stats by it: a placed block counts as "blocks placed", a used item does not. So blocks of
 * Minecraft versions newer than MCConnect's lists, of mods (hybrid servers add them to Material) and of data packs
 * (Paper's registries) end up in the right place.
 */
final class RegistryReporter {
    /** Resource locations as MCConnect accepts them; anything else is left out. */
    private static final Pattern NAME = Pattern.compile("^[a-z0-9_.-]{1,64}:[a-z0-9_./-]{1,128}$");

    private static String cached;

    private RegistryReporter() {
    }

    /** {"version", "blocks", "items"}; built once (the registries do not change while the server runs). */
    static synchronized String json(String version) {
        if (cached != null) return cached;
        Set<String> blocks = new TreeSet<>();
        Set<String> items = new TreeSet<>();
        for (Material material : Material.values()) {
            try {
                if (material.isLegacy()) continue;
                String key = material.getKey().toString();
                if (material.isBlock()) blocks.add(key);
                else if (material.isItem()) items.add(key);
            } catch (Throwable ignored) {
                // an entry a hybrid server added without a key
            }
        }
        addRegistry("BLOCK", blocks);
        addRegistry("ITEM", items);
        items.removeAll(blocks);
        StringBuilder json = new StringBuilder("{\"version\":\"")
                .append(version == null ? "" : version.replaceAll("[\"\\\\\\p{Cntrl}]", "")).append('"');
        appendList(json, "blocks", blocks);
        appendList(json, "items", items);
        cached = json.append('}').toString();
        return cached;
    }

    /**
     * Registry.BLOCK / Registry.ITEM (BlockType, ItemType: Paper 1.20.6+, newer Spigot), which also hold content of
     * data packs and mods that Material lacks. By reflection: the plugin is built against the 1.13 API.
     */
    private static void addRegistry(String field, Set<String> names) {
        try {
            Object registry = Class.forName("org.bukkit.Registry").getField(field).get(null);
            for (Object entry : (Iterable<?>) registry) {
                try {
                    if (entry instanceof Keyed) names.add(((Keyed) entry).getKey().toString());
                } catch (Throwable ignored) {
                    // an entry without a key
                }
            }
        } catch (Throwable ignored) {
            // older server: Material only
        }
    }

    private static void appendList(StringBuilder json, String name, Set<String> values) {
        json.append(",\"").append(name).append("\":[");
        boolean first = true;
        for (String value : values) {
            if (!NAME.matcher(value).matches()) continue;
            if (!first) json.append(',');
            json.append('"').append(value).append('"');
            first = false;
        }
        json.append(']');
    }
}
