package org.tobias.mcdatalink.api;

import org.bukkit.entity.Player;

/** A chat line in MCConnect's markup, parsed once and sent per viewer (with or without the icons). */
public interface ChatLine {

    void send(Player player);

    /** To every player online. */
    void broadcast();
}
