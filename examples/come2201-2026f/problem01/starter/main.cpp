#include <iostream>
#include <memory>
#include <string>
#include <vector>

// TODO: BaseFormatter, BracketDecorator, AlertDecorator implement this interface.
class Formatter {
public:
    virtual ~Formatter() = default;
    virtual std::string format(int value) const = 0;
};

class Observer {
public:
    virtual ~Observer() = default;
    virtual const std::string& id() const = 0;
    virtual void update(int value) const = 0;
};

// TODO: Implement DisplayObserver using a composed Formatter.
class SensorSubject {
public:
    bool subscribe(std::shared_ptr<Observer> observer) {
        (void)observer; return false; // TODO: reject duplicate identifiers.
    }
    bool unsubscribe(const std::string& id) {
        (void)id; return false; // TODO: preserve remaining subscription order.
    }
    void publish(int value) const {
        (void)value; // TODO: notify each observer, or print NO_OBSERVERS.
    }
private:
    std::vector<std::shared_ptr<Observer>> observers_;
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
            // TODO: validate id/style; compose Formatter; register DisplayObserver.
        } else if (command == "UNSUB") {
            std::cin >> id;
            // TODO: unsubscribe and print the specified result.
        } else if (command == "PUBLISH") {
            int value = 0;
            std::cin >> value;
            sensor.publish(value);
        }
    }
}
