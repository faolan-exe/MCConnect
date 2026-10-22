package org.tobias.mcdatalink.api;

/**
 * An MCConnect addon: a normal Bukkit plugin that registers itself with {@link MCConnect#register} in its onEnable.
 * Every callback runs on the main server thread.
 *
 * An addon does nothing until the server owner approved it on the MCConnect website (official addons from the
 * MCConnect store are approved automatically). Its settings live in MCConnect and are edited on the website; the
 * last ones are cached by MCDataLink, so an approved addon also works while MCConnect is not reachable.
 */
public interface Addon {

    /**
     * The addon is approved and these are its settings (every key of its settings description, missing values
     * filled with the defaults). Called again whenever the settings change: apply them completely.
     */
    void onActivate(AddonConfig config);

    /** Not (or no longer) approved, or paused by the owner: switch every function off. */
    void onDeactivate();

    /** A message from MCConnect for this addon (only while active). */
    default void onMessage(String message) {
    }
}
