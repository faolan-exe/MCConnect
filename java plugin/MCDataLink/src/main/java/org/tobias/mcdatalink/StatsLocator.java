package org.tobias.mcdatalink;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileVisitOption;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Arrays;
import java.util.EnumSet;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;

/**
 * Finds the folder with the players' stats files (&lt;uuid&gt;.json).
 *
 * Classic worlds keep them in world/stats. Newer versions store the overworld in
 * world/dimensions/minecraft/overworld and the player data in world/players, so the
 * parent folders are checked and, as a last resort, the world folder is searched.
 * A folder only counts if it contains a &lt;uuid&gt;.json with statistics (advancement
 * files are named the same way).
 */
final class StatsLocator {
    private static final String[] CANDIDATES = {"stats", "players/stats", "players"};
    private static final Set<String> SEARCH_NAMES = new HashSet<>(Arrays.asList("stats", "players"));
    /** Chunk data folders can be huge and never contain stats. */
    private static final Set<String> SKIP = new HashSet<>(Arrays.asList("region", "entities", "poi", "data", "datapacks"));
    private static final int MAX_SEARCH_DEPTH = 5;

    private StatsLocator() {
    }

    /** @return the stats folder, or null if none exists (yet). */
    static File find(File worldFolder, File worldContainer) {
        File container = normalize(worldContainer);
        File levelRoot = normalize(worldFolder);
        for (File dir = levelRoot; dir != null && !dir.equals(container); dir = dir.getParentFile()) {
            for (String candidate : CANDIDATES) {
                File stats = new File(dir, candidate);
                if (containsStats(stats)) return stats;
            }
            if (new File(dir, "level.dat").isFile()) levelRoot = dir;
        }
        return search(levelRoot);
    }

    private static File search(File root) {
        final File[] result = new File[1];
        try {
            Files.walkFileTree(root.toPath(), EnumSet.noneOf(FileVisitOption.class), MAX_SEARCH_DEPTH,
                    new SimpleFileVisitor<Path>() {
                        @Override
                        public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                            String name = dir.getFileName() == null ? "" : dir.getFileName().toString();
                            if (SKIP.contains(name)) return FileVisitResult.SKIP_SUBTREE;
                            if (SEARCH_NAMES.contains(name) && containsStats(dir.toFile())) {
                                result[0] = dir.toFile();
                                return FileVisitResult.TERMINATE;
                            }
                            return FileVisitResult.CONTINUE;
                        }
                    });
        } catch (IOException ignored) {
            // unreadable folder: treat as "no stats yet"
        }
        return result[0];
    }

    /** True if the folder has a &lt;uuid&gt;.json whose content is a stats file. */
    static boolean containsStats(File dir) {
        File[] files = dir.listFiles((d, n) -> n.endsWith(".json") && isUuid(n.substring(0, n.length() - 5)));
        if (files == null) return false;
        for (File file : files) {
            try {
                String head = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
                if (head.contains("\"stats\"")) return true;
            } catch (IOException ignored) {
                // try the next file
            }
        }
        return false;
    }

    private static boolean isUuid(String value) {
        try {
            UUID.fromString(value);
            return value.length() == 36;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    private static File normalize(File file) {
        return file.toPath().toAbsolutePath().normalize().toFile();
    }
}
