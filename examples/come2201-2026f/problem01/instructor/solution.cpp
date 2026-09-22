#include <algorithm>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

class Formatter {
public:
    virtual ~Formatter() = default;
    virtual std::string format(int value) const = 0;
};
class BaseFormatter final : public Formatter {
public:
    std::string format(int value) const override { return "value=" + std::to_string(value); }
};
class FormatterDecorator : public Formatter {
protected:
    std::shared_ptr<Formatter> wrapped_;
public:
    explicit FormatterDecorator(std::shared_ptr<Formatter> wrapped) : wrapped_(std::move(wrapped)) {}
};
class BracketDecorator final : public FormatterDecorator {
public:
    using FormatterDecorator::FormatterDecorator;
    std::string format(int value) const override { return "[" + wrapped_->format(value) + "]"; }
};
class AlertDecorator final : public FormatterDecorator {
public:
    using FormatterDecorator::FormatterDecorator;
    std::string format(int value) const override { return "!" + wrapped_->format(value) + "!"; }
};
class Observer {
public:
    virtual ~Observer() = default;
    virtual const std::string& id() const = 0;
    virtual void update(int value) const = 0;
};
class DisplayObserver final : public Observer {
    std::string id_;
    std::shared_ptr<Formatter> formatter_;
public:
    DisplayObserver(std::string id, std::shared_ptr<Formatter> formatter)
        : id_(std::move(id)), formatter_(std::move(formatter)) {}
    const std::string& id() const override { return id_; }
    void update(int value) const override {
        std::cout << id_ << ": " << formatter_->format(value) << "\n";
    }
};
class SensorSubject {
    std::vector<std::shared_ptr<Observer>> observers_;
public:
    bool contains(const std::string& id) const {
        return std::any_of(observers_.begin(), observers_.end(),
                           [&](const auto& observer) { return observer->id() == id; });
    }
    bool subscribe(std::shared_ptr<Observer> observer) {
        if (contains(observer->id())) return false;
        observers_.push_back(std::move(observer));
        return true;
    }
    bool unsubscribe(const std::string& id) {
        auto found = std::find_if(observers_.begin(), observers_.end(),
                                 [&](const auto& observer) { return observer->id() == id; });
        if (found == observers_.end()) return false;
        observers_.erase(found);
        return true;
    }
    void publish(int value) const {
        if (observers_.empty()) std::cout << "NO_OBSERVERS\n";
        for (const auto& observer : observers_) observer->update(value);
    }
};
int main() {
    int count = 0;
    std::cin >> count;
    SensorSubject sensor;
    for (int i = 0; i < count; ++i) {
        std::string command, id, style;
        std::cin >> command;
        if (command == "SUB") {
            std::cin >> id >> style;
            if (sensor.contains(id)) { std::cout << "ERROR duplicate\n"; continue; }
            if (style != "-" && (style.empty() || style.size() > 4 ||
                style.find_first_not_of("AB") != std::string::npos)) {
                std::cout << "ERROR style\n"; continue;
            }
            std::shared_ptr<Formatter> formatter = std::make_shared<BaseFormatter>();
            if (style != "-") for (char operation : style) {
                if (operation == 'B') formatter = std::make_shared<BracketDecorator>(formatter);
                else formatter = std::make_shared<AlertDecorator>(formatter);
            }
            sensor.subscribe(std::make_shared<DisplayObserver>(id, formatter));
            std::cout << "OK " << id << "\n";
        } else if (command == "UNSUB") {
            std::cin >> id;
            std::cout << (sensor.unsubscribe(id) ? "OK " + id : "ERROR missing") << "\n";
        } else if (command == "PUBLISH") {
            int value = 0;
            std::cin >> value;
            sensor.publish(value);
        }
    }
}
