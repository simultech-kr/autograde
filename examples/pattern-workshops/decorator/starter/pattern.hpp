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
    std::string description() const override {
        // TODO 1
        throw std::logic_error("TODO: Americano::description");
    }
    int cost() const override {
        // TODO 2
        throw std::logic_error("TODO: Americano::cost");
    }
};

class CafeLatte final : public Beverage {
public:
    std::string description() const override {
        // TODO 3
        throw std::logic_error("TODO: CafeLatte::description");
    }
    int cost() const override {
        // TODO 4
        throw std::logic_error("TODO: CafeLatte::cost");
    }
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
    std::string description() const override {
        // TODO 5: Delegate, then append this option.
        throw std::logic_error("TODO: Milk::description");
    }
    int cost() const override {
        // TODO 6: Delegate, then add this option's price.
        throw std::logic_error("TODO: Milk::cost");
    }
};

class Shot final : public BeverageDecorator {
public:
    using BeverageDecorator::BeverageDecorator;
    std::string description() const override {
        // TODO 7
        throw std::logic_error("TODO: Shot::description");
    }
    int cost() const override {
        // TODO 8
        throw std::logic_error("TODO: Shot::cost");
    }
};

class WhippedCream final : public BeverageDecorator {
public:
    using BeverageDecorator::BeverageDecorator;
    std::string description() const override {
        // TODO 9
        throw std::logic_error("TODO: WhippedCream::description");
    }
    int cost() const override {
        // TODO 10
        throw std::logic_error("TODO: WhippedCream::cost");
    }
};
