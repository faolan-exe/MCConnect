package org.tobias.mcconnect.easynewcraftingrecipes;

import org.bukkit.ChatColor;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.inventory.FurnaceRecipe;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.Recipe;
import org.bukkit.inventory.ShapedRecipe;
import org.bukkit.inventory.ShapelessRecipe;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.java.JavaPlugin;
import org.tobias.mcdatalink.api.Addon;
import org.tobias.mcdatalink.api.AddonConfig;
import org.tobias.mcdatalink.api.AddonHandle;
import org.tobias.mcdatalink.api.MCConnect;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Own crafting and furnace recipes (the old easyNewCraftingRecipes as an MCConnect addon). The recipes come from the
 * settings and are replaced completely on every change, without a restart. Fixed against the old plugin: a broken
 * recipe names its problem (shown on the website) instead of "Error in Crafting Recipe", shapeless and furnace
 * recipes, and the shape is trimmed so a 2×2 recipe also works in the inventory's crafting grid.
 */
public final class EasyNewCraftingRecipes extends JavaPlugin implements Addon, Listener {
    private AddonHandle handle;
    private final List<NamespacedKey> registered = new ArrayList<>();
    private boolean discover;

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). EasyNewCraftingRecipes needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(this, this);
    }

    @Override
    public void onDisable() {
        removeAll();
    }

    @Override
    public void onActivate(AddonConfig config) {
        removeAll();
        discover = config.getBoolean("discover", true);
        List<String> problems = new ArrayList<>();
        Set<String> keys = new HashSet<>();
        for (AddonConfig row : config.getList("recipes")) {
            if (!row.getBoolean("enabled", true)) continue;
            String name = row.getString("name", "Rezept");
            String problem;
            try {
                problem = add(row, uniqueKey(name, keys));
            } catch (RuntimeException e) {  // e.g. a recipe the server refuses
                problem = e.getMessage();
            }
            if (problem != null) problems.add("Rezept »" + name + "«: " + problem);
        }
        handle.reportProblems(problems);
        updateRecipes();
        if (discover) {
            for (Player player : getServer().getOnlinePlayers()) discoverAll(player);
        }
        getLogger().info(registered.size() + " own recipes active");
    }

    @Override
    public void onDeactivate() {
        removeAll();
        updateRecipes();
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        if (discover) discoverAll(event.getPlayer());
    }

    // ------------------------------------------------------------------ recipes

    /** Adds one recipe; returns the problem, or null if it worked. */
    private String add(AddonConfig row, NamespacedKey key) {
        Material resultType = material(row.getString("result", ""));
        if (resultType == null) return itemProblem(row.getString("result", ""), "Ergebnis");
        ItemStack result = new ItemStack(resultType, Math.max(1, Math.min(64, row.getInt("amount", 1))));
        String customName = row.getString("result_name", "");
        if (!customName.isEmpty()) {
            ItemMeta meta = result.getItemMeta();
            if (meta != null) {
                meta.setDisplayName(ChatColor.translateAlternateColorCodes('&', customName));
                result.setItemMeta(meta);
            }
        }
        String type = row.getString("type", "shaped");
        Recipe recipe;
        if (type.equals("furnace")) {
            Material input = material(row.getString("input", ""));
            if (input == null) return itemProblem(row.getString("input", ""), "Zutat");
            int ticks = (int) Math.round(Math.max(1, Math.min(600, row.getDouble("cook_seconds", 10))) * 20);
            recipe = new FurnaceRecipe(key, result, input, (float) Math.max(0, row.getDouble("experience", 0.1)), ticks);
        } else {
            List<String> grid = row.getStringList("grid");
            Material[] slots = new Material[9];
            boolean any = false;
            for (int i = 0; i < 9; i++) {
                String id = i < grid.size() ? grid.get(i).trim() : "";
                if (id.isEmpty()) continue;
                slots[i] = material(id);
                if (slots[i] == null) return itemProblem(id, "Zutat");
                any = true;
            }
            if (!any) return "Im Raster liegt keine Zutat.";
            recipe = type.equals("shapeless") ? shapeless(key, result, slots) : shaped(key, result, slots);
        }
        if (!getServer().addRecipe(recipe)) return "Der Server hat das Rezept abgelehnt (gibt es dieses Rezept schon?).";
        registered.add(key);
        return null;
    }

    private static ShapelessRecipe shapeless(NamespacedKey key, ItemStack result, Material[] slots) {
        ShapelessRecipe recipe = new ShapelessRecipe(key, result);
        for (Material slot : slots) {
            if (slot != null) recipe.addIngredient(slot);
        }
        return recipe;
    }

    /** The grid without empty rows and columns at the edges (a 2×2 shape fits the inventory). */
    private static ShapedRecipe shaped(NamespacedKey key, ItemStack result, Material[] slots) {
        int top = 3, bottom = -1, left = 3, right = -1;
        for (int i = 0; i < 9; i++) {
            if (slots[i] == null) continue;
            top = Math.min(top, i / 3);
            bottom = Math.max(bottom, i / 3);
            left = Math.min(left, i % 3);
            right = Math.max(right, i % 3);
        }
        Map<Material, Character> symbols = new LinkedHashMap<>();
        String[] rows = new String[bottom - top + 1];
        for (int row = top; row <= bottom; row++) {
            StringBuilder line = new StringBuilder();
            for (int column = left; column <= right; column++) {
                Material slot = slots[row * 3 + column];
                if (slot == null) {
                    line.append(' ');
                } else {
                    Character symbol = symbols.get(slot);
                    if (symbol == null) {
                        symbol = (char) ('A' + symbols.size());
                        symbols.put(slot, symbol);
                    }
                    line.append(symbol);
                }
            }
            rows[row - top] = line.toString();
        }
        ShapedRecipe recipe = new ShapedRecipe(key, result);
        recipe.shape(rows);
        for (Map.Entry<Material, Character> symbol : symbols.entrySet()) recipe.setIngredient(symbol.getValue(), symbol.getKey());
        return recipe;
    }

    private void removeAll() {
        if (registered.isEmpty()) return;
        Set<NamespacedKey> keys = new HashSet<>(registered);
        for (Player player : getServer().getOnlinePlayers()) {
            for (NamespacedKey key : keys) player.undiscoverRecipe(key);
        }
        boolean removed = false;
        try {  // 1.15+: Server.removeRecipe
            java.lang.reflect.Method remove = getServer().getClass().getMethod("removeRecipe", NamespacedKey.class);
            for (NamespacedKey key : keys) remove.invoke(getServer(), key);
            removed = true;
        } catch (ReflectiveOperationException | LinkageError ignored) {
            // older servers: through the iterator
        }
        if (!removed) {
            Iterator<Recipe> recipes = getServer().recipeIterator();
            while (recipes.hasNext()) {
                Recipe recipe = recipes.next();
                NamespacedKey key = recipe instanceof ShapedRecipe ? ((ShapedRecipe) recipe).getKey()
                        : recipe instanceof ShapelessRecipe ? ((ShapelessRecipe) recipe).getKey()
                        : recipe instanceof FurnaceRecipe ? ((FurnaceRecipe) recipe).getKey() : null;
                if (key != null && keys.contains(key)) recipes.remove();
            }
        }
        registered.clear();
    }

    /** Paper: send the changed recipes to the players' clients (recipe book). */
    private void updateRecipes() {
        try {
            getServer().getClass().getMethod("updateRecipes").invoke(getServer());
        } catch (ReflectiveOperationException | LinkageError ignored) {
            // Spigot: the clients get them on the next join
        }
    }

    private void discoverAll(Player player) {
        for (NamespacedKey key : registered) player.discoverRecipe(key);
    }

    // ------------------------------------------------------------------ helpers

    private NamespacedKey uniqueKey(String name, Set<String> taken) {
        String base = name.toLowerCase(Locale.ROOT).replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
                .replaceAll("[^a-z0-9]+", "_").replaceAll("^_+|_+$", "");
        if (base.isEmpty()) base = "recipe";
        String key = base;
        for (int i = 2; !taken.add(key); i++) key = base + "_" + i;
        return new NamespacedKey(this, key);
    }

    private static Material material(String id) {
        if (id == null || id.trim().isEmpty()) return null;
        Material material = Material.matchMaterial(id.trim());
        return material != null && material.isItem() ? material : null;
    }

    private static String itemProblem(String id, String what) {
        return id.trim().isEmpty() ? what + " fehlt."
                : what + " »" + id + "« gibt es in dieser Minecraft-Version nicht (oder es ist kein Item).";
    }
}
