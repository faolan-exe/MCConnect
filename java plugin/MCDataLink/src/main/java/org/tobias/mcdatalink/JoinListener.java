package org.tobias.mcdatalink;

import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.AsyncPlayerChatEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.world.WorldSaveEvent;

public class JoinListener implements Listener {
    private final MCDataLink plugin;

    public JoinListener(MCDataLink plugin) {
        this.plugin = plugin;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerJoin(PlayerJoinEvent event) {
        plugin.prefixDisplay().apply(event.getPlayer());
        plugin.nameBadges().joined(event.getPlayer());
        plugin.playerJoined(event.getPlayer());
    }

    @EventHandler(priority = EventPriority.HIGH, ignoreCancelled = true)
    @SuppressWarnings("deprecation")  // still fired by Paper for plugins that listen to it
    public void onChat(AsyncPlayerChatEvent event) {
        event.setFormat(plugin.prefixDisplay().chatFormat(event.getPlayer().getUniqueId(), event.getFormat()));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerQuit(PlayerQuitEvent event) {
        plugin.playerQuit(event.getPlayer());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onWorldSave(WorldSaveEvent event) {
        if (plugin.isMainWorld(event.getWorld())) {
            plugin.mainWorldSaved();
        }
    }
}
