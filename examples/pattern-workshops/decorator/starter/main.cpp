#include "pattern.hpp"
#include <iostream>

int main() {
    try {
        std::unique_ptr<Beverage> first = std::make_unique<Americano>();
        first = std::make_unique<Milk>(std::move(first));
        first = std::make_unique<Shot>(std::move(first));
        std::cout << first->description() << " = " << first->cost() << " KRW\n";
        std::unique_ptr<Beverage> second = std::make_unique<CafeLatte>();
        second = std::make_unique<Shot>(std::move(second));
        second = std::make_unique<Shot>(std::move(second));
        second = std::make_unique<WhippedCream>(std::move(second));
        std::cout << second->description() << " = " << second->cost() << " KRW\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
