package org.tobias.mcconnect.easynewcraftingrecipes;

import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.Material;
import org.bukkit.enchantments.Enchantment;
import org.bukkit.inventory.ItemFlag;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.FireworkMeta;
import org.bukkit.inventory.meta.ItemMeta;

import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Results with extras: the presets of the settings and own item data like /give.
 *
 * Item data: since 1.20.5 the server parses "item[components]" itself (ItemFactory.createItemStack, called by
 * reflection since this addon compiles against the 1.13 API); before, "item{nbt}" goes through
 * UnsafeValues.modifyItemStack. Presets use the Bukkit API where it has the extra (enchantments, unbreakable,
 * firework power) and entity data otherwise, in the syntax of the running version.
 */
final class SpecialItems {
    /** Thrown with a German text for the website. */
    static final class Problem extends Exception {
        Problem(String message) {
            super(message);
        }
    }

    private static final Pattern GIVE = Pattern.compile("^([a-z0-9_.:-]+)?\\s*(.*)$", Pattern.DOTALL);

    private SpecialItems() {
    }

    // ------------------------------------------------------------------ item data like /give

    /**
     * The item of a /give text. fallback: the item id when the text starts with "[" or "{" (the result of the recipe).
     */
    static ItemStack parse(String data, Material fallback) throws Problem {
        String text = data.trim();
        if (text.startsWith("/give ") || text.startsWith("give ")) {  // pasted with the command and its target
            text = text.substring(text.indexOf(' ') + 1).trim();
            int space = text.indexOf(' ');
            text = space < 0 ? "" : text.substring(space + 1).trim().replaceFirst("\\s+\\d+$", "");  // without the count
        }
        Matcher matcher = GIVE.matcher(text);
        if (!matcher.matches()) throw new Problem("Die Item-Daten sind nicht lesbar.");
        String id = matcher.group(1), extra = matcher.group(2).trim();
        if (id == null || id.isEmpty()) {
            if (fallback == null) throw new Problem("Die Item-Daten beginnen mit [ oder {, aber oben ist kein Ergebnis gewählt.");
            id = fallback.getKey().getKey();
        }
        Method create = createItemStack();
        if (create != null) {
            try {
                return (ItemStack) create.invoke(Bukkit.getItemFactory(), id + extra);
            } catch (ReflectiveOperationException e) {
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                throw new Problem("Item-Daten ungültig: " + clean(cause.getMessage()));
            }
        }
        Material material = Material.matchMaterial(id);
        if (material == null || !material.isItem()) throw new Problem("Das Item »" + id + "« gibt es in dieser Minecraft-Version nicht.");
        ItemStack stack = new ItemStack(material);
        if (extra.isEmpty()) return stack;
        if (extra.startsWith("[")) {
            throw new Problem("Die Schreibweise mit [Komponenten] gibt es erst ab Minecraft 1.20.5. Hier: item{NBT}.");
        }
        try {
            @SuppressWarnings("deprecation")
            ItemStack modified = Bukkit.getUnsafe().modifyItemStack(stack, extra);
            return modified;
        } catch (RuntimeException e) {
            throw new Problem("Item-Daten ungültig: " + clean(e.getMessage()));
        }
    }

    /** ItemFactory.createItemStack(String) of 1.20.5+, or null. */
    private static Method createItemStack() {
        try {
            return Bukkit.getItemFactory().getClass().getMethod("createItemStack", String.class);
        } catch (NoSuchMethodException | LinkageError e) {
            return null;
        }
    }

    /** Components since 1.20.5 (createItemStack exists then). */
    private static boolean components() {
        return createItemStack() != null;
    }

    private static String clean(String message) {
        if (message == null || message.trim().isEmpty()) return "der Server versteht sie nicht.";
        String text = message.replaceAll("\\s+", " ").trim();
        return text.length() > 160 ? text.substring(0, 160) + " …" : text;
    }

    // ------------------------------------------------------------------ presets

    /** What happens when a player uses the item (the addon handles it). */
    enum Use { NONE, FIREBALL, SMALL_FIREBALL }

    static Use use(String preset) {
        return "fireball".equals(preset) ? Use.FIREBALL : "small_fireball".equals(preset) ? Use.SMALL_FIREBALL : Use.NONE;
    }

    /** The item of a preset, or null for "none". */
    static ItemStack preset(String preset) throws Problem {
        switch (preset == null ? "none" : preset) {
            case "none":
            case "":
                return null;
            case "invisible_item_frame":
                return named(entity("item_frame", "Invisible:1b"), "Unsichtbarer Item Frame");
            case "invisible_glow_item_frame":
                return named(entity("glow_item_frame", "Invisible:1b"), "Unsichtbarer leuchtender Item Frame");
            case "invisible_armor_stand":
                return named(entity("armor_stand", "Invisible:1b"), "Unsichtbarer Rüstungsständer");
            case "armor_stand_arms":
                return named(entity("armor_stand", "ShowArms:1b"), "Rüstungsständer mit Armen");
            case "small_armor_stand":
                return named(entity("armor_stand", "Small:1b,ShowArms:1b"), "Kleiner Rüstungsständer");
            case "knockback_stick": {
                ItemStack stick = new ItemStack(Material.STICK);
                stick.addUnsafeEnchantment(Enchantment.KNOCKBACK, 5);
                return named(stick, "Knockback-Stick");
            }
            case "fireball":
                return thrown(Material.FIRE_CHARGE, "Feuerball", "Rechtsklick: werfen – explodiert beim Aufprall");
            case "small_fireball":
                return thrown(Material.FIRE_CHARGE, "Feuerkugel", "Rechtsklick: werfen – setzt in Brand");
            case "unbreakable_elytra": {
                ItemStack elytra = new ItemStack(Material.ELYTRA);
                ItemMeta meta = elytra.getItemMeta();
                meta.setUnbreakable(true);
                elytra.setItemMeta(meta);
                return named(elytra, "Unzerbrechliche Elytra");
            }
            case "rocket_3": {
                ItemStack rocket = new ItemStack(Material.FIREWORK_ROCKET);
                FireworkMeta meta = (FireworkMeta) rocket.getItemMeta();
                meta.setPower(3);
                rocket.setItemMeta(meta);
                return rocket;
            }
            default:
                throw new Problem("Das Spezial-Item »" + preset + "« kennt diese Version der Erweiterung nicht.");
        }
    }

    /** A spawn item (item frame, armor stand) whose entity gets the tags, in the syntax of the running version. */
    private static ItemStack entity(String item, String tags) throws Problem {
        if (Material.matchMaterial(item) == null) throw new Problem("»" + item + "« gibt es in dieser Minecraft-Version nicht.");
        String data = components()
                ? item + "[entity_data={id:\"minecraft:" + item + "\"," + tags + "}]"
                : item + "{EntityTag:{" + tags + "}}";
        return parse(data, null);
    }

    private static ItemStack thrown(Material material, String name, String hint) {
        ItemStack item = new ItemStack(material);
        ItemMeta meta = item.getItemMeta();
        meta.setLore(Arrays.asList(ChatColor.GRAY + hint));
        item.setItemMeta(meta);
        item.addUnsafeEnchantment(Enchantment.DURABILITY, 1);  // the glint (hidden below)
        meta = item.getItemMeta();
        meta.addItemFlags(ItemFlag.HIDE_ENCHANTS);
        item.setItemMeta(meta);
        return named(item, name);
    }

    private static ItemStack named(ItemStack item, String name) {
        ItemMeta meta = item.getItemMeta();
        if (meta != null && !meta.hasDisplayName()) {
            meta.setDisplayName(ChatColor.RESET + name);
            item.setItemMeta(meta);
        }
        return item;
    }
}
