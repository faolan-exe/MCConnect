package org.tobias.mcdatalink.api;

import com.google.gson.JsonObject;
import org.bukkit.entity.Player;

import java.util.List;

/** What an addon can do with MCConnect, returned by {@link MCConnect#register}. Use it on the main thread. */
public interface AddonHandle {

    /** The addon's id from its mcconnect-addon.json. */
    String id();

    /** Approved and not paused: the addon may work. */
    boolean isActive();

    /** MCDataLink is connected to MCConnect right now. */
    boolean isConnected();

    /** The current settings, or null while the addon is not active. */
    AddonConfig config();

    /** A message to the addon's handler on MCConnect. False if not active or not connected. */
    boolean send(String message);

    /**
     * Change the addon's own settings, e.g. from an in-game command. MCConnect checks them against the settings
     * description, stores them (they show up on the website) and sends them back: they only apply with the next
     * {@link Addon#onActivate}. actor (may be null) is named in the moderation log. False if not active or not connected.
     */
    boolean saveConfig(JsonObject config, Player actor);

    /** Problems with the settings (e.g. an item this Minecraft version does not know), shown on the website. */
    void reportProblems(List<String> problems);

    /** Run an MCConnect in-game command for a player (answered in the chat). False if not connected. */
    boolean command(Player player, String name, String args);

    /**
     * A chat line with MCConnect's markup: "&amp;" colors, icons of the resource pack (with fallbacks for players
     * without it), buttons and tooltips.
     */
    ChatLine chat(String markup);

    /** The markup without colors, buttons and icons, e.g. for the console. */
    String plain(String markup);
}
