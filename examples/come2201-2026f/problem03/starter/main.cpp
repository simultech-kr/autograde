#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

// Legacy APIs: keep names, signatures and raw units unchanged.
class CelsiusSensor {
    std::vector<int> samples_;
public:
    explicit CelsiusSensor(std::vector<int> samples) : samples_(std::move(samples)) {}
    const std::vector<int>& readTenths() const { return samples_; }
};
class FahrenheitSensor {
    std::vector<int> samples_;
public:
    explicit FahrenheitSensor(std::vector<int> samples) : samples_(std::move(samples)) {}
    const std::vector<int>& readWholeDegrees() const { return samples_; }
};
class TemperatureSource {
public:
    virtual ~TemperatureSource() = default;
    virtual std::vector<double> readCelsius() const = 0;
};
// TODO: Add CelsiusAdapter and FahrenheitAdapter implementing TemperatureSource.
class Report {
public:
    virtual ~Report() = default;
    void run(const TemperatureSource& source) const {
        (void)source;
        // TODO: fixed template method: read -> filter with keep() -> summarize -> output.
    }
protected:
    virtual bool keep(double celsius) const = 0;
};
// TODO: Add AllReport and NonnegativeReport implementing only the keep hook.
int main() {
    std::string sensor, policy;
    int count = 0;
    std::cin >> sensor >> policy >> count;
    std::vector<int> raw(static_cast<std::size_t>(count));
    for (int& value : raw) std::cin >> value;
    // TODO: validate sensor/policy, select an adapter and a Report, then call run.
}
