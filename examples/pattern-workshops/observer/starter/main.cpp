#include "pattern.hpp"
#include <iostream>

int main() {
    try {
        AssignmentBoard board;
        auto inbox = std::make_shared<InboxObserver>();
        auto counter = std::make_shared<CountObserver>();
        board.subscribe(inbox);
        board.subscribe(counter);
        board.publish({"lab03", 30});
        board.unsubscribe(counter);
        board.publish({"lab03", 10});
        for (const auto& message : inbox->messages()) std::cout << message << '\n';
        std::cout << "notifications=" << counter->count() << '\n';
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
