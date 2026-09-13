#pragma once
#include <algorithm>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

struct Notice {
    std::string assignment;
    int minutes_remaining;
};

class Observer {
public:
    virtual ~Observer() = default;
    virtual void update(const Notice& notice) = 0;
};

class InboxObserver final : public Observer {
public:
    void update(const Notice& notice) override {
        // TODO 1: Store the formatted notice in messages_.
        (void)notice;
        throw std::logic_error("TODO: InboxObserver::update");
    }
    const std::vector<std::string>& messages() const { return messages_; }
private:
    std::vector<std::string> messages_;
};

class CountObserver final : public Observer {
public:
    void update(const Notice& notice) override {
        // TODO 2: Count one received notification.
        (void)notice;
        throw std::logic_error("TODO: CountObserver::update");
    }
    int count() const { return count_; }
private:
    int count_ = 0;
};

class AssignmentBoard {
public:
    void subscribe(const std::shared_ptr<Observer>& observer) {
        // TODO 3: Ignore null, avoid duplicates, keep only weak ownership.
        (void)observer;
        throw std::logic_error("TODO: AssignmentBoard::subscribe");
    }
    void unsubscribe(const std::shared_ptr<Observer>& observer) {
        // TODO 4: Remove this observer; removing an absent observer is safe.
        (void)observer;
        throw std::logic_error("TODO: AssignmentBoard::unsubscribe");
    }
    void publish(const Notice& notice) {
        // TODO 5: Notify live subscribers once, through Observer::update.
        (void)notice;
        throw std::logic_error("TODO: AssignmentBoard::publish");
    }
private:
    std::vector<std::weak_ptr<Observer>> observers_;
};
