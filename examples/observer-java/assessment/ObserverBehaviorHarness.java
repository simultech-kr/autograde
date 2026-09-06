import edu.autograde.observer.CurrentConditionsDisplay;
import edu.autograde.observer.Observer;
import edu.autograde.observer.WeatherSnapshot;
import edu.autograde.observer.WeatherStation;

import java.util.LinkedHashMap;
import java.util.Map;

/** Instructor-owned behavioral checks. This source is not part of the starter. */
public final class ObserverBehaviorHarness {
    private static final double EPSILON = 0.000001;
    private static final Map<String, Boolean> RESULTS = new LinkedHashMap<>();

    private ObserverBehaviorHarness() {
    }

    @FunctionalInterface
    private interface Check {
        void run() throws Exception;
    }

    private static final class RecordingObserver implements Observer {
        private int count;
        private WeatherSnapshot latest;

        @Override
        public void update(WeatherSnapshot snapshot) {
            count += 1;
            latest = snapshot;
        }
    }

    private static void require(boolean condition) {
        if (!condition) {
            throw new AssertionError();
        }
    }

    private static void requireSnapshot(
            WeatherSnapshot snapshot,
            double temperature,
            double humidity,
            double pressure) {
        require(snapshot != null);
        require(Math.abs(snapshot.getTemperature() - temperature) < EPSILON);
        require(Math.abs(snapshot.getHumidity() - humidity) < EPSILON);
        require(Math.abs(snapshot.getPressure() - pressure) < EPSILON);
    }

    private static void check(String name, Check check) {
        try {
            check.run();
            RESULTS.put(name, true);
        } catch (Throwable ignored) {
            RESULTS.put(name, false);
        }
    }

    public static void main(String[] args) {
        if (args.length != 7) {
            System.exit(2);
        }
        String token = args[0];
        double t1 = Double.parseDouble(args[1]);
        double h1 = Double.parseDouble(args[2]);
        double p1 = Double.parseDouble(args[3]);
        double t2 = Double.parseDouble(args[4]);
        double h2 = Double.parseDouble(args[5]);
        double p2 = Double.parseDouble(args[6]);

        check("notification", () -> {
            WeatherStation station = new WeatherStation();
            RecordingObserver observer = new RecordingObserver();
            station.registerObserver(observer);
            station.setMeasurements(t1, h1, p1);
            require(observer.count == 1);
            requireSnapshot(observer.latest, t1, h1, p1);
            requireSnapshot(station.getCurrentSnapshot(), t1, h1, p1);
            station.setMeasurements(t2, h2, p2);
            require(observer.count == 2);
            requireSnapshot(observer.latest, t2, h2, p2);
        });

        check("lifecycle", () -> {
            WeatherStation station = new WeatherStation();
            RecordingObserver observer = new RecordingObserver();
            station.removeObserver(observer);
            station.registerObserver(observer);
            station.registerObserver(observer);
            station.setMeasurements(t1, h1, p1);
            require(observer.count == 1);
            station.removeObserver(observer);
            station.removeObserver(observer);
            station.setMeasurements(t2, h2, p2);
            require(observer.count == 1);
        });

        check("multiple", () -> {
            WeatherStation station = new WeatherStation();
            RecordingObserver first = new RecordingObserver();
            RecordingObserver second = new RecordingObserver();
            station.registerObserver(first);
            station.registerObserver(second);
            station.setMeasurements(t1, h1, p1);
            require(first.count == 1);
            require(second.count == 1);
            requireSnapshot(first.latest, t1, h1, p1);
            requireSnapshot(second.latest, t1, h1, p1);
        });

        check("display", () -> {
            WeatherStation station = new WeatherStation();
            CurrentConditionsDisplay display = new CurrentConditionsDisplay();
            station.registerObserver(display);
            station.setMeasurements(t2, h2, p2);
            require(display.getUpdateCount() == 1);
            requireSnapshot(display.getLatestSnapshot(), t2, h2, p2);
        });

        for (Map.Entry<String, Boolean> entry : RESULTS.entrySet()) {
            System.out.println(
                    token + "\t" + entry.getKey() + "\t"
                            + (entry.getValue() ? "PASS" : "FAIL"));
        }
    }
}

