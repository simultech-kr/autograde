package edu.autograde.observer;

import java.util.ArrayList;
import java.util.List;

/** Subject whose Observer Pattern implementation is completed by the student. */
public final class WeatherStation implements Subject {
    private final List<Observer> observers = new ArrayList<>();
    private WeatherSnapshot currentSnapshot;

    @Override
    public void registerObserver(Observer observer) {
        // TODO: register each observer instance at most once.
    }

    @Override
    public void removeObserver(Observer observer) {
        // TODO: safely remove an observer when it is present.
    }

    @Override
    public void notifyObservers() {
        // TODO: notify every currently registered observer.
    }

    public void setMeasurements(double temperature, double humidity, double pressure) {
        currentSnapshot = new WeatherSnapshot(temperature, humidity, pressure);
        // TODO: notify observers after changing the state.
    }

    public WeatherSnapshot getCurrentSnapshot() {
        return currentSnapshot;
    }
}

