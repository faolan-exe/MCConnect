package org.tobias.mcconnect.spawnelytra;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

/** The values of /spawnelytra edit: absolute ones must lie in the range, relative ones (+/-) are kept in it. */
public class ChangeByTest {
    private static final double DELTA = 1e-9;

    @Test
    public void absoluteInRange() {
        assertEquals(2.5, SpawnElytra.changeBy("2.5", 1, 0, 5), DELTA);
        assertEquals(2.5, SpawnElytra.changeBy("2,5", 1, 0, 5), DELTA);  // German comma
    }

    @Test
    public void absoluteBordersIncluded() {
        assertEquals(0, SpawnElytra.changeBy("0", 1, 0, 5), DELTA);
        assertEquals(5, SpawnElytra.changeBy("5", 1, 0, 5), DELTA);
    }

    @Test
    public void absoluteOutsideRefused() {
        assertNull(SpawnElytra.changeBy("5.1", 1, 0, 5));
        assertNull(SpawnElytra.changeBy("1", 20, 2, 500));
    }

    @Test
    public void relativeAddsAndSubtracts() {
        assertEquals(25, SpawnElytra.changeBy("+5", 20, 2, 500), DELTA);
        assertEquals(0.5, SpawnElytra.changeBy("-0.5", 1, 0, 5), DELTA);
    }

    @Test
    public void relativeKeptInRange() {
        assertEquals(0, SpawnElytra.changeBy("-0.5", 0.2, 0, 5), DELTA);
        assertEquals(5, SpawnElytra.changeBy("+0.5", 4.8, 0, 5), DELTA);
        assertEquals(2, SpawnElytra.changeBy("-5", 4, 2, 500), DELTA);
    }

    @Test
    public void notANumberRefused() {
        assertNull(SpawnElytra.changeBy("viel", 1, 0, 5));
        assertNull(SpawnElytra.changeBy("NaN", 1, 0, 5));
        assertNull(SpawnElytra.changeBy("+Infinity", 1, 0, 5));
        assertNull(SpawnElytra.changeBy("", 1, 0, 5));
    }
}
