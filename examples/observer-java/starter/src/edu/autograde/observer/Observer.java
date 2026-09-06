package edu.autograde.observer;

/** Receives an immutable snapshot whenever the subject changes. */
public interface Observer {
    void update(WeatherSnapshot snapshot);
}

