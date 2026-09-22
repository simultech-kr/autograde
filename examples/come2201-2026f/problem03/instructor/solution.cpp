#include <algorithm>
#include <iomanip>
#include <iostream>
#include <memory>
#include <numeric>
#include <string>
#include <utility>
#include <vector>

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
class CelsiusAdapter final : public TemperatureSource {
    CelsiusSensor sensor_;
public:
    explicit CelsiusAdapter(std::vector<int> samples) : sensor_(std::move(samples)) {}
    std::vector<double> readCelsius() const override {
        std::vector<double> result;
        for (int raw : sensor_.readTenths()) result.push_back(raw / 10.0);
        return result;
    }
};
class FahrenheitAdapter final : public TemperatureSource {
    FahrenheitSensor sensor_;
public:
    explicit FahrenheitAdapter(std::vector<int> samples) : sensor_(std::move(samples)) {}
    std::vector<double> readCelsius() const override {
        std::vector<double> result;
        for (int raw : sensor_.readWholeDegrees()) result.push_back((raw - 32) * 5.0 / 9.0);
        return result;
    }
};
class Report {
public:
    virtual ~Report() = default;
    void run(const TemperatureSource& source) const {
        std::vector<double> kept;
        for (double value : source.readCelsius()) if (keep(value)) kept.push_back(value);
        std::cout << "COUNT " << kept.size() << "\n";
        if (kept.empty()) { std::cout << "NO_DATA\n"; return; }
        std::cout << std::fixed << std::setprecision(1)
                  << "MIN " << *std::min_element(kept.begin(), kept.end()) << "\n"
                  << "MAX " << *std::max_element(kept.begin(), kept.end()) << "\n"
                  << "MEAN " << std::accumulate(kept.begin(), kept.end(), 0.0) / kept.size() << "\n";
    }
protected:
    virtual bool keep(double celsius) const = 0;
};
class AllReport final : public Report {
protected:
    bool keep(double) const override { return true; }
};
class NonnegativeReport final : public Report {
protected:
    bool keep(double celsius) const override { return celsius >= 0.0; }
};
int main() {
    std::string sensor, policy;
    int count = 0;
    std::cin >> sensor >> policy >> count;
    std::vector<int> raw(static_cast<std::size_t>(count));
    for (int& value : raw) std::cin >> value;
    if (sensor != "C" && sensor != "F") { std::cout << "ERROR sensor\n"; return 0; }
    if (policy != "ALL" && policy != "NONNEGATIVE") { std::cout << "ERROR policy\n"; return 0; }
    std::unique_ptr<TemperatureSource> source;
    if (sensor == "C") source = std::make_unique<CelsiusAdapter>(raw);
    else source = std::make_unique<FahrenheitAdapter>(raw);
    std::unique_ptr<Report> report;
    if (policy == "ALL") report = std::make_unique<AllReport>();
    else report = std::make_unique<NonnegativeReport>();
    report->run(*source);
}
