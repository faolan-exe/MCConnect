package org.tobias.mcdatalink.api;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * Entry point for addons. Put "depend: [MCDataLink]" into the addon's plugin.yml and a mcconnect-addon.json
 * (id, name, description, settings) next to it, then register in onEnable:
 *
 * <pre>
 *   handle = MCConnect.register(this, new Addon() { ... });
 * </pre>
 *
 * MCDataLink unregisters the addon by itself when its plugin is disabled.
 */
public final class MCConnect {
    /** Implemented by MCDataLink (set while it is enabled). */
    public interface Registry {
        AddonHandle register(JavaPlugin plugin, Addon addon);
    }

    private static volatile Registry registry;

    private MCConnect() {
    }

    /**
     * Register the addon. Throws IllegalStateException if MCDataLink is not enabled, IllegalArgumentException if the
     * plugin has no valid mcconnect-addon.json, the addon class is not the plugin's own or the id is taken.
     */
    public static AddonHandle register(JavaPlugin plugin, Addon addon) {
        Registry current = registry;
        if (current == null) throw new IllegalStateException("MCDataLink is not enabled");
        return current.register(plugin, addon);
    }

    public static boolean available() {
        return registry != null;
    }

    /** Only for MCDataLink. */
    public static void setRegistry(Registry value) {
        registry = value;
    }
}
