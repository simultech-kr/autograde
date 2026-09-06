package edu.autograde.observer;

/** Immutable measurements delivered to observers. */
public final class WeatherSnapshot {
    private final double temperature;
    private final double humidity;
    private final double pressure;

    public WeatherSnapshot(double temperature, double humidity, double pressure) {
        this.temperature = temperature;
        this.humidity = humidity;
        this.pressure = pressure;
    }

    public double getTemperature() {
        return temperature;
    }

    public double getHumidity() {
        return humidity;
    }

    public double getPressure() {
        return pressure;
    }
}

