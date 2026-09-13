#pragma once
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

class Beverage {
public:
    virtual ~Beverage() = default;
    virtual std::string description() const = 0;
    virtual int cost() const = 0;
};
class Americano final : public Beverage {
public:
    std::string description() const override { return "Americano"; }
    int cost() const override { return 2000; }
};
class CafeLatte final : public Beverage {
public:
    std::string description() const override { return "Cafe Latte"; }
    int cost() const override { return 3000; }
};
class BeverageDecorator : public Beverage {
public:
    explicit BeverageDecorator(std::unique_ptr<Beverage> component)
        : component_(std::move(component)) {
        if (!component_) throw std::invalid_argument("beverage must not be null");
    }
protected:
    std::unique_ptr<Beverage> component_;
};
class Milk final : public BeverageDecorator {
public:
    using BeverageDecorator::BeverageDecorator;
    std::string description() const override { return component_->description() + " + Milk"; }
    int cost() const override { return component_->cost() + 500; }
};
class Shot final : public BeverageDecorator {
public:
    using BeverageDecorator::BeverageDecorator;
    std::string description() const override { return component_->description() + " + Shot"; }
    int cost() const override { return component_->cost() + 700; }
};
class WhippedCream final : public BeverageDecorator {
public:
    using BeverageDecorator::BeverageDecorator;
    std::string description() const override { return component_->description() + " + Whipped Cream"; }
    int cost() const override { return component_->cost() + 600; }
};
