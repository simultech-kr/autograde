#pragma once
#include <algorithm>
#include <memory>
#include <string>
#include <vector>

struct Notice { std::string assignment; int minutes_remaining; };
class Observer {
public:
    virtual ~Observer() = default;
    virtual void update(const Notice& notice) = 0;
};
class InboxObserver final : public Observer {
public:
    void update(const Notice& notice) override {
        messages_.push_back(notice.assignment + " due in " + std::to_string(notice.minutes_remaining) + " min");
    }
    const std::vector<std::string>& messages() const { return messages_; }
private:
    std::vector<std::string> messages_;
};
class CountObserver final : public Observer {
public:
    void update(const Notice&) override { ++count_; }
    int count() const { return count_; }
private:
    int count_ = 0;
};
class AssignmentBoard {
public:
    void subscribe(const std::shared_ptr<Observer>& observer) {
        if (!observer) return;
        observers_.erase(std::remove_if(observers_.begin(), observers_.end(),
            [](const auto& item) { return item.expired(); }), observers_.end());
        for (const auto& item : observers_) if (item.lock() == observer) return;
        observers_.push_back(observer);
    }
    void unsubscribe(const std::shared_ptr<Observer>& observer) {
        observers_.erase(std::remove_if(observers_.begin(), observers_.end(),
            [&](const auto& item) { return item.expired() || item.lock() == observer; }), observers_.end());
    }
    void publish(const Notice& notice) {
        // Copy weak handles so callback-induced vector changes cannot invalidate iteration.
        const auto snapshot = observers_;
        for (const auto& item : snapshot) if (auto observer = item.lock()) observer->update(notice);
    }
private:
    std::vector<std::weak_ptr<Observer>> observers_;
};
