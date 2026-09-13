#include "pattern.hpp"
#include <functional>
#include <iostream>

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

int main() {
    int passed = 0;
    const auto run = [&](const char* name, const std::function<void()>& test) {
        try { test(); ++passed; std::cout << "PASS " << name << '\n'; }
        catch (const std::exception& e) { std::cout << "FAIL " << name << ": " << e.what() << '\n'; }
    };
    run("base drinks", [] {
        Americano coffee; CafeLatte latte;
        require(coffee.cost() == 2000 && coffee.description() == "Americano", "Americano contract");
        require(latte.cost() == 3000 && latte.description() == "Cafe Latte", "CafeLatte contract");
    });
    run("nested options", [] {
        Shot drink(std::make_unique<Milk>(std::make_unique<Americano>()));
        require(drink.cost() == 3200, "price must accumulate");
        require(drink.description() == "Americano + Milk + Shot", "description must accumulate");
    });
    run("repeated options and wrapping order", [] {
        WhippedCream drink(std::make_unique<Shot>(std::make_unique<Shot>(std::make_unique<CafeLatte>())));
        require(drink.cost() == 5000, "repeated shot price");
        require(drink.description() == "Cafe Latte + Shot + Shot + Whipped Cream", "repeated shot description");
        Milk reverse(std::make_unique<Shot>(std::make_unique<Americano>()));
        require(reverse.cost() == 3200 && reverse.description() == "Americano + Shot + Milk", "wrapping order contract");
    });
    run("unknown base drink", [] {
        struct Tea final : Beverage {
            std::string description() const override { return "Tea"; }
            int cost() const override { return 4100; }
        };
        Milk drink(std::make_unique<WhippedCream>(std::make_unique<Shot>(std::make_unique<Tea>())));
        require(drink.cost() == 5900, "must delegate to any Beverage");
        require(drink.description() == "Tea + Shot + Whipped Cream + Milk", "must preserve unknown base description");
    });
    run("null and destruction", [] {
        bool rejected = false;
        try { Shot invalid(nullptr); } catch (const std::invalid_argument&) { rejected = true; }
        require(rejected, "null must be rejected");
        int destroyed = 0;
        struct Tracked final : Beverage {
            int& destroyed;
            explicit Tracked(int& value) : destroyed(value) {}
            ~Tracked() override { ++destroyed; }
            std::string description() const override { return "Tracked"; }
            int cost() const override { return 1; }
        };
        { std::unique_ptr<Beverage> chain = std::make_unique<Milk>(std::make_unique<Tracked>(destroyed)); }
        require(destroyed == 1, "owned base must be destroyed exactly once");
    });
    std::cout << passed << "/5 checks passed\n";
    return passed == 5 ? 0 : 1;
}
