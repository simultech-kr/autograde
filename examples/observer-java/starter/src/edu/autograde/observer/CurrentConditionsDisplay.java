package edu.autograde.observer;

/** A concrete observer that remembers the latest current conditions. */
public final class CurrentConditionsDisplay implements Observer {
    private WeatherSnapshot latestSnapshot;
    private int updateCount;

    @Override
    public void update(WeatherSnapshot snapshot) {
        // TODO: remember the snapshot and increase updateCount.
    }

    public WeatherSnapshot getLatestSnapshot() {
        return latestSnapshot;
    }

    public int getUpdateCount() {
        return updateCount;
    }
}

