package org.tobias.mcconnect.easyheaddrop;

import org.bukkit.ChatColor;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.enchantments.Enchantment;
import org.bukkit.entity.Entity;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.Player;
import org.bukkit.entity.Projectile;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.inventory.meta.SkullMeta;
import org.bukkit.plugin.java.JavaPlugin;
import org.tobias.mcdatalink.api.Addon;
import org.tobias.mcdatalink.api.AddonConfig;
import org.tobias.mcdatalink.api.AddonHandle;
import org.tobias.mcdatalink.api.MCConnect;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Heads as trophies (the old EasyHeadDrop as an MCConnect addon). Fixed against the old plugin: the killer of a
 * ranged kill is known (it used the last damager, an arrow), no commons-lang (gone on newer Paper), no blocking name
 * lookup for the skin (the head gets the player itself), the head also drops with keepInventory, and a cooldown keeps
 * players from farming each other's heads.
 */
public final class EasyHeadDrop extends JavaPlugin implements Addon, Listener {
    /** entity type name -> head material name (matched at runtime: newer heads are missing on old servers) */
    private static final Map<String, String> MOB_HEADS = new HashMap<>();

    static {
        MOB_HEADS.put("ZOMBIE", "ZOMBIE_HEAD");
        MOB_HEADS.put("SKELETON", "SKELETON_SKULL");
        MOB_HEADS.put("CREEPER", "CREEPER_HEAD");
        MOB_HEADS.put("WITHER_SKELETON", "WITHER_SKELETON_SKULL");
        MOB_HEADS.put("PIGLIN", "PIGLIN_HEAD");
        MOB_HEADS.put("ENDER_DRAGON", "DRAGON_HEAD");
    }

    private AddonHandle handle;
    private volatile AddonConfig settings;
    /** victim:killer -> when the last head dropped */
    private final Map<String, Long> lastDrop = new HashMap<>();

    @Override
    public void onEnable() {
        if (!MCConnect.available()) {
            getLogger().severe("MCDataLink is not enabled (no server key?). EasyHeadDrop needs it.");
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        handle = MCConnect.register(this, this);
        getServer().getPluginManager().registerEvents(this, this);
    }

    @Override
    public void onActivate(AddonConfig config) {
        settings = config;
        List<String> problems = new ArrayList<>();
        try {
            new SimpleDateFormat(config.getString("date_format", "dd.MM.yyyy"), Locale.GERMANY);
        } catch (IllegalArgumentException e) {
            problems.add("Das Datumsformat ist ungültig, es wird dd.MM.yyyy benutzt.");
        }
        for (String type : config.getStringList("mob_types")) {
            String head = MOB_HEADS.get(type.toUpperCase(Locale.ROOT));
            if (config.getBoolean("mob_heads", false) && head != null && Material.matchMaterial(head) == null) {
                problems.add("Köpfe von " + type + " gibt es in dieser Minecraft-Version noch nicht.");
            }
        }
        handle.reportProblems(problems);
    }

    @Override
    public void onDeactivate() {
        settings = null;
    }

    // ------------------------------------------------------------------ players

    @EventHandler(priority = EventPriority.HIGH)
    public void onPlayerDeath(PlayerDeathEvent event) {
        AddonConfig config = settings;
        if (config == null || !config.getBoolean("player_heads", true)) return;
        Player victim = event.getEntity();
        if (!worldAllowed(config, victim)) return;
        Player killer = victim.getKiller();  // also set for arrows, tridents and the like
        if (killer == victim) killer = null;
        if (killer == null && config.getBoolean("require_player_killer", true)) return;
        if (!onCooldown(config, victim, killer) && roll(config.getDouble("chance", 100) + lootingLevel(killer) * config.getDouble("looting_bonus", 0))) {
            String weapon = weaponOf(victim, killer);
            String killerName = killer != null ? killer.getName() : config.getString("no_killer", "–");
            ItemStack head = playerHead(config, victim, killerName, weapon);
            if (event.getKeepInventory()) victim.getWorld().dropItemNaturally(victim.getLocation(), head);
            else event.getDrops().add(head);
            if (killer != null) {
                long now = System.currentTimeMillis();
                lastDrop.values().removeIf(at -> now - at > 24 * 3_600_000L);  // longer than the longest cooldown
                lastDrop.put(victim.getUniqueId() + ":" + killer.getUniqueId(), now);
            }
            if (config.getBoolean("broadcast", false)) {
                handle.chat(config.getString("broadcast_text", "").replace("{victim}", victim.getName())
                        .replace("{killer}", killerName).replace("{weapon}", weapon)).broadcast();
            }
        }
    }

    private boolean onCooldown(AddonConfig config, Player victim, Player killer) {
        int minutes = config.getInt("cooldown_minutes", 10);
        if (killer == null || minutes <= 0) return false;
        Long last = lastDrop.get(victim.getUniqueId() + ":" + killer.getUniqueId());
        return last != null && System.currentTimeMillis() - last < minutes * 60_000L;
    }

    private ItemStack playerHead(AddonConfig config, Player victim, String killerName, String weapon) {
        ItemStack head = new ItemStack(Material.PLAYER_HEAD);
        SkullMeta meta = (SkullMeta) head.getItemMeta();
        if (meta == null) return head;
        meta.setOwningPlayer(victim);  // the skin of the player itself, no lookup by name
        meta.setDisplayName(color(config.getString("name", "{victim}").replace("{victim}", victim.getName())));
        String date = date(config.getString("date_format", "dd.MM.yyyy"));
        EntityDamageEvent cause = victim.getLastDamageCause();
        List<String> lore = new ArrayList<>();
        for (String line : config.getStringList("lore")) {
            lore.add(color(line.replace("{victim}", victim.getName()).replace("{killer}", killerName).replace("{weapon}", weapon)
                    .replace("{date}", date).replace("{world}", victim.getWorld().getName())
                    .replace("{cause}", cause == null ? "–" : pretty(cause.getCause().name()))));
        }
        meta.setLore(lore);
        head.setItemMeta(meta);
        return head;
    }

    /** The name of the item in the killer's hand ("Fist" without one), for ranged kills the bow, crossbow or trident. */
    private static String weaponOf(Player victim, Player killer) {
        if (killer == null) return "–";
        ItemStack item = killer.getInventory().getItemInMainHand();
        EntityDamageEvent last = victim.getLastDamageCause();
        if (last instanceof EntityDamageByEntityEvent && ((EntityDamageByEntityEvent) last).getDamager() instanceof Projectile) {
            Entity projectile = ((EntityDamageByEntityEvent) last).getDamager();
            String type = item.getType().name();
            if (!type.equals("BOW") && !type.equals("CROSSBOW") && !type.equals("TRIDENT")) return pretty(projectile.getType().name());
        }
        if (item.getType() == Material.AIR) return "Fist";
        ItemMeta meta = item.getItemMeta();
        if (meta != null && meta.hasDisplayName()) return meta.getDisplayName();
        return pretty(item.getType().name());
    }

    private static int lootingLevel(Player killer) {
        if (killer == null) return 0;
        Enchantment looting = Enchantment.getByKey(NamespacedKey.minecraft("looting"));  // renamed field in 1.20.5
        return looting == null ? 0 : killer.getInventory().getItemInMainHand().getEnchantmentLevel(looting);
    }

    // ------------------------------------------------------------------ mobs

    @EventHandler(priority = EventPriority.HIGH)
    public void onMobDeath(EntityDeathEvent event) {
        AddonConfig config = settings;
        LivingEntity entity = event.getEntity();
        if (config == null || entity instanceof Player || !config.getBoolean("mob_heads", false)) return;
        if (!worldAllowed(config, entity)) return;
        String type = entity.getType().name();
        Set<String> chosen = new HashSet<>();
        for (String name : config.getStringList("mob_types")) chosen.add(name.toUpperCase(Locale.ROOT));
        String headName = MOB_HEADS.get(type);
        if (headName == null || !chosen.contains(type)) return;
        Material head = Material.matchMaterial(headName);
        Player killer = entity.getKiller();
        if (head == null || (killer == null && config.getBoolean("mob_require_player", true))) return;
        if (roll(config.getDouble("mob_chance", 5) + lootingLevel(killer) * config.getDouble("looting_bonus", 0))) {
            event.getDrops().add(new ItemStack(head));
        }
    }

    // ------------------------------------------------------------------ helpers

    private static boolean worldAllowed(AddonConfig config, Entity entity) {
        List<String> worlds = config.getStringList("worlds");
        if (worlds.isEmpty()) return true;
        for (String world : worlds) {
            if (world.trim().equalsIgnoreCase(entity.getWorld().getName())) return true;
        }
        return false;
    }

    private static boolean roll(double percent) {
        return percent >= 100 || ThreadLocalRandom.current().nextDouble(100) < percent;
    }

    private static String date(String pattern) {
        try {
            return new SimpleDateFormat(pattern, Locale.GERMANY).format(new Date());
        } catch (IllegalArgumentException e) {
            return new SimpleDateFormat("dd.MM.yyyy", Locale.GERMANY).format(new Date());
        }
    }

    /** DIAMOND_SWORD -> Diamond Sword (the English names players know). */
    static String pretty(String name) {
        StringBuilder result = new StringBuilder();
        for (String word : name.toLowerCase(Locale.ROOT).split("_")) {
            if (word.isEmpty()) continue;
            if (result.length() > 0) result.append(' ');
            result.append(Character.toUpperCase(word.charAt(0))).append(word.substring(1));
        }
        return result.toString();
    }

    private static String color(String text) {
        return ChatColor.translateAlternateColorCodes('&', text);
    }
}
