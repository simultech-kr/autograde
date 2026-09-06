#include "observer.hpp"

#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using observer_lab::Observer;
using observer_lab::Subject;

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

class RecordingObserver final : public Observer {
public:
    RecordingObserver(
        std::string name,
        std::vector<std::string>& events,
        const Subject* subject = nullptr
    )
        : name_(std::move(name)), events_(events), subject_(subject) {}

    void update(int state) override {
        events_.push_back(name_ + ":" + std::to_string(state));
        if (subject_ != nullptr) {
            observed_subject_states_.push_back(subject_->state());
        }
    }

    const std::vector<int>& observedSubjectStates() const {
        return observed_subject_states_;
    }

private:
    std::string name_;
    std::vector<std::string>& events_;
    const Subject* subject_;
    std::vector<int> observed_subject_states_;
};

class MutatingObserver final : public Observer {
public:
    MutatingObserver(
        Subject& subject,
        Observer& observer_to_detach,
        Observer& observer_to_attach,
        std::vector<std::string>& events
    )
        : subject_(subject),
          observer_to_detach_(observer_to_detach),
          observer_to_attach_(observer_to_attach),
          events_(events) {}

    void update(int state) override {
        events_.push_back("mutating:" + std::to_string(state));
        subject_.detach(&observer_to_detach_);
        subject_.attach(&observer_to_attach_);
    }

private:
    Subject& subject_;
    Observer& observer_to_detach_;
    Observer& observer_to_attach_;
    std::vector<std::string>& events_;
};

void basicNotification() {
    Subject subject;
    std::vector<std::string> events;
    RecordingObserver observer("one", events, &subject);

    require(subject.state() == 0, "initial state must be zero");
    subject.attach(&observer);
    subject.setState(7);

    require(events == std::vector<std::string>{"one:7"}, "observer did not receive state 7");
    require(
        observer.observedSubjectStates() == std::vector<int>{7},
        "subject state was not updated before notification"
    );
}

void multipleObserversInOrder() {
    Subject subject;
    std::vector<std::string> events;
    RecordingObserver alpha("alpha", events);
    RecordingObserver beta("beta", events);
    RecordingObserver gamma("gamma", events);

    subject.attach(&alpha);
    subject.attach(&beta);
    subject.attach(&gamma);
    subject.setState(11);
    subject.setState(-4);

    const std::vector<std::string> expected{
        "alpha:11", "beta:11", "gamma:11",
        "alpha:-4", "beta:-4", "gamma:-4",
    };
    require(events == expected, "observers were not called once in registration order");
}

void duplicateAndNullRegistration() {
    Subject subject;
    std::vector<std::string> events;
    RecordingObserver observer("only", events);

    subject.attach(nullptr);
    subject.attach(&observer);
    subject.attach(&observer);
    subject.attach(nullptr);
    subject.attach(&observer);
    subject.setState(23);

    require(events == std::vector<std::string>{"only:23"}, "duplicate observer was notified");
}

void detachIsSafe() {
    Subject subject;
    std::vector<std::string> events;
    RecordingObserver removed("removed", events);
    RecordingObserver retained("retained", events);

    subject.attach(&removed);
    subject.attach(&retained);
    subject.detach(nullptr);
    subject.detach(&removed);
    subject.detach(&removed);
    subject.setState(31);
    subject.detach(&retained);
    subject.setState(32);

    require(events == std::vector<std::string>{"retained:31"}, "detach behavior is incorrect");
}

void notificationUsesSnapshot() {
    Subject subject;
    std::vector<std::string> events;
    RecordingObserver removed_during_notify("removed", events);
    RecordingObserver added_during_notify("added", events);
    MutatingObserver mutating(
        subject,
        removed_during_notify,
        added_during_notify,
        events
    );

    subject.attach(&mutating);
    subject.attach(&removed_during_notify);
    subject.setState(41);
    subject.setState(42);

    const std::vector<std::string> expected{
        "mutating:41", "removed:41", "mutating:42", "added:42",
    };
    require(events == expected, "subscription mutation did not use notification snapshot semantics");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "exactly one test case is required\n";
        return 2;
    }

    const std::string selected(argv[1]);
    const std::vector<std::pair<std::string, std::function<void()>>> cases{
        {"basic", basicNotification},
        {"multiple", multipleObserversInOrder},
        {"duplicate", duplicateAndNullRegistration},
        {"detach", detachIsSafe},
        {"snapshot", notificationUsesSnapshot},
    };

    for (const auto& [name, test] : cases) {
        if (selected != name) {
            continue;
        }
        try {
            test();
            std::cout << "PASS\n";
            return 0;
        } catch (const std::exception& error) {
            std::cout << "FAIL: " << error.what() << '\n';
            return 1;
        } catch (...) {
            std::cout << "FAIL: unknown exception\n";
            return 1;
        }
    }

    std::cerr << "unknown test case\n";
    return 2;
}
