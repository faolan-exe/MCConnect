package org.tobias.mcdatalink.api;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * An addon's settings (a JSON object checked by MCConnect against the addon's settings description). The getters
 * never throw: a missing or wrongly typed value gives the default.
 */
public final class AddonConfig {
    private final JsonObject json;

    public AddonConfig(JsonObject json) {
        this.json = json == null ? new JsonObject() : json;
    }

    public static AddonConfig parse(String text) {
        try {
            JsonElement element = new JsonParser().parse(text);
            return new AddonConfig(element.isJsonObject() ? element.getAsJsonObject() : null);
        } catch (RuntimeException e) {
            return new AddonConfig(null);
        }
    }

    public boolean has(String key) {
        return json.has(key) && !json.get(key).isJsonNull();
    }

    public boolean getBoolean(String key, boolean fallback) {
        JsonElement value = json.get(key);
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isBoolean() ? value.getAsBoolean() : fallback;
    }

    public int getInt(String key, int fallback) {
        JsonElement value = json.get(key);
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isNumber() ? value.getAsInt() : fallback;
    }

    public double getDouble(String key, double fallback) {
        JsonElement value = json.get(key);
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isNumber() ? value.getAsDouble() : fallback;
    }

    public String getString(String key, String fallback) {
        JsonElement value = json.get(key);
        return value != null && value.isJsonPrimitive() && value.getAsJsonPrimitive().isString() ? value.getAsString() : fallback;
    }

    /** A list of texts (e.g. "lines" or "multiselect" settings); empty if missing. */
    public List<String> getStringList(String key) {
        JsonElement value = json.get(key);
        if (value == null || !value.isJsonArray()) return Collections.emptyList();
        List<String> result = new ArrayList<>();
        for (JsonElement item : value.getAsJsonArray()) {
            result.add(item.isJsonPrimitive() ? item.getAsString() : "");
        }
        return result;
    }

    /** The rows of a "list" setting; empty if missing. */
    public List<AddonConfig> getList(String key) {
        JsonElement value = json.get(key);
        if (value == null || !value.isJsonArray()) return Collections.emptyList();
        List<AddonConfig> result = new ArrayList<>();
        for (JsonElement item : value.getAsJsonArray()) {
            if (item.isJsonObject()) result.add(new AddonConfig(item.getAsJsonObject()));
        }
        return result;
    }

    /** A "position" setting as {x, y, z}, or null if missing. */
    public int[] getPosition(String key) {
        JsonElement value = json.get(key);
        if (value == null || !value.isJsonObject()) return null;
        AddonConfig position = new AddonConfig(value.getAsJsonObject());
        if (!position.has("x") || !position.has("y") || !position.has("z")) return null;
        return new int[]{position.getInt("x", 0), position.getInt("y", 0), position.getInt("z", 0)};
    }

    /** A copy of the JSON, e.g. to change it and hand it to {@link AddonHandle#saveConfig}. */
    public JsonObject toJson() {
        return new JsonParser().parse(json.toString()).getAsJsonObject();  // deepCopy() is not public in Gson 2.8.0 (1.13)
    }

    public static JsonObject position(int x, int y, int z) {
        JsonObject position = new JsonObject();
        position.addProperty("x", x);
        position.addProperty("y", y);
        position.addProperty("z", z);
        return position;
    }

    public static JsonArray strings(List<String> values) {
        JsonArray array = new JsonArray();
        for (String value : values) array.add(value);
        return array;
    }

    @Override
    public boolean equals(Object other) {
        return other instanceof AddonConfig && ((AddonConfig) other).json.equals(json);
    }

    @Override
    public int hashCode() {
        return json.hashCode();
    }

    @Override
    public String toString() {
        return json.toString();
    }
}
