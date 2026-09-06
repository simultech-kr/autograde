package edu.autograde.observer;

/** Minimal subject contract used by the assignment. */
public interface Subject {
    void registerObserver(Observer observer);

    void removeObserver(Observer observer);

    void notifyObservers();
}

