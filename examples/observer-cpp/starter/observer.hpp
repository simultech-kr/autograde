#pragma once

#include <vector>

namespace observer_lab {

class Observer {
public:
    virtual ~Observer() = default;
    virtual void update(int state) = 0;
};

class Subject {
public:
    void attach(Observer* observer);
    void detach(Observer* observer);
    void setState(int new_state);
    [[nodiscard]] int state() const noexcept;

private:
    void notify();

    int state_{0};
    std::vector<Observer*> observers_;
};

}  // namespace observer_lab
