#include "observer.hpp"

#include <iostream>
#include <string>
#include <utility>

namespace {

class ConsoleObserver final : public observer_lab::Observer {
public:
    explicit ConsoleObserver(std::string name) : name_(std::move(name)) {}

    void update(int state) override {
        std::cout << name_ << ": " << state << '\n';
    }

private:
    std::string name_;
};

}  // namespace

int main() {
    observer_lab::Subject subject;
    ConsoleObserver display_a("display-a");
    ConsoleObserver display_b("display-b");

    subject.attach(&display_a);
    subject.attach(&display_b);
    subject.setState(10);
    subject.detach(&display_a);
    subject.setState(20);
}
