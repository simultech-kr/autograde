#include "pattern.hpp"
#include <functional>
#include <iostream>
#include <stdexcept>

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

int main() {
    int passed = 0;
    const auto run = [&](const char* name, const std::function<void()>& test) {
        try { test(); ++passed; std::cout << "PASS " << name << '\n'; }
        catch (const std::exception& e) { std::cout << "FAIL " << name << ": " << e.what() << '\n'; }
    };
    run("broadcast and order", [] {
        AssignmentBoard board;
        auto inbox = std::make_shared<InboxObserver>();
        auto count = std::make_shared<CountObserver>();
        board.subscribe(inbox); board.subscribe(count);
        board.publish({"lab03", 30}); board.publish({"lab03", 10});
        require(inbox->messages() == std::vector<std::string>{"lab03 due in 30 min", "lab03 due in 10 min"}, "notice values/order differ");
        require(count->count() == 2, "counter must receive both notices");
    });
    run("deduplication and unsubscribe", [] {
        AssignmentBoard board;
        auto first = std::make_shared<CountObserver>();
        auto second = std::make_shared<CountObserver>();
        board.subscribe(first); board.subscribe(first); board.subscribe(second);
        board.publish({"lab", 20});
        board.unsubscribe(first); board.unsubscribe(first);
        board.publish({"lab", 5});
        require(first->count() == 1 && second->count() == 2, "duplicate or unsubscribe failure");
        board.subscribe(first); board.publish({"lab", 0});
        require(first->count() == 2 && second->count() == 3, "resubscribe failure");
    });
    run("null and expired subscriber", [] {
        AssignmentBoard board;
        board.subscribe(nullptr); board.unsubscribe(nullptr); board.publish({"empty", 0});
        std::weak_ptr<Observer> lifetime;
        {
            auto temporary = std::make_shared<CountObserver>();
            lifetime = temporary; board.subscribe(temporary);
        }
        require(lifetime.expired(), "board must not own subscriber lifetime");
        board.publish({"expired", 0});
    });
    run("new observer without modifying board", [] {
        struct Spy final : Observer {
            int id; std::vector<int>& order;
            Spy(int value, std::vector<int>& output) : id(value), order(output) {}
            void update(const Notice&) override { order.push_back(id); }
        };
        AssignmentBoard board; std::vector<int> order;
        auto one = std::make_shared<Spy>(1, order);
        auto two = std::make_shared<Spy>(2, order);
        board.subscribe(one); board.subscribe(two); board.publish({"custom", 1});
        require(order == std::vector<int>{1, 2}, "must notify abstract observers in registration order");
    });
    std::cout << passed << "/4 checks passed\n";
    return passed == 4 ? 0 : 1;
}
