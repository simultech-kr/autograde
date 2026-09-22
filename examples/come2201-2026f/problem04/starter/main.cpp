#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

class Mediator {
public:
    virtual ~Mediator() = default;
    virtual void broadcast(const std::string& sender, const std::string& message) const = 0;
};
class Participant {
    std::string name_;
    const Mediator& mediator_;
public:
    Participant(std::string name, const Mediator& mediator)
        : name_(std::move(name)), mediator_(mediator) {}
    const std::string& name() const { return name_; }
    void send(const std::string& message) const {
        (void)message; (void)mediator_; // TODO: delegate to mediator_.
    }
    void receive(const std::string& sender, const std::string& message) const {
        (void)sender; (void)message; // TODO: output the required delivery line.
    }
};
class MemberIterator {
public:
    explicit MemberIterator(const std::vector<std::shared_ptr<Participant>>& members) {
        (void)members; // TODO: store traversal state.
    }
    bool hasNext() const { return false; }
    std::shared_ptr<Participant> next() { return nullptr; } // TODO.
};
class RoomMediator : public Mediator {
    std::vector<std::shared_ptr<Participant>> members_;
public:
    void join(const std::string& name) { (void)name; } // TODO.
    void leave(const std::string& name) { (void)name; } // TODO.
    void send(const std::string& sender, const std::string& message) const {
        (void)sender; (void)message; // TODO: find sender and invoke Participant::send.
    }
    void list() const {} // TODO: use MemberIterator.
    void broadcast(const std::string& sender, const std::string& message) const override {
        (void)sender; (void)message; // TODO: iterate recipients, excluding sender.
    }
};
int main() {
    int count = 0;
    std::cin >> count;
    RoomMediator room;
    while (count-- > 0) {
        std::string command, name, message;
        std::cin >> command;
        if (command == "JOIN") { std::cin >> name; room.join(name); }
        else if (command == "LEAVE") { std::cin >> name; room.leave(name); }
        else if (command == "SEND") { std::cin >> name >> message; room.send(name, message); }
        else if (command == "LIST") room.list();
    }
}
