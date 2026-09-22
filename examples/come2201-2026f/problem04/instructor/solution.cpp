#include <algorithm>
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
    void send(const std::string& message) const { mediator_.broadcast(name_, message); }
    void receive(const std::string& sender, const std::string& message) const {
        std::cout << name_ << "<-" << sender << ": " << message << "\n";
    }
};
class MemberIterator {
    const std::vector<std::shared_ptr<Participant>>& members_;
    std::size_t position_ = 0;
public:
    explicit MemberIterator(const std::vector<std::shared_ptr<Participant>>& members)
        : members_(members) {}
    bool hasNext() const { return position_ < members_.size(); }
    std::shared_ptr<Participant> next() { return members_.at(position_++); }
};
class RoomMediator final : public Mediator {
    std::vector<std::shared_ptr<Participant>> members_;
    std::shared_ptr<Participant> find(const std::string& name) const {
        MemberIterator it(members_);
        while (it.hasNext()) {
            auto member = it.next();
            if (member->name() == name) return member;
        }
        return nullptr;
    }
public:
    void join(const std::string& name) {
        if (find(name)) { std::cout << "ERROR duplicate\n"; return; }
        members_.push_back(std::make_shared<Participant>(name, *this));
        std::cout << "JOINED " << name << "\n";
    }
    void leave(const std::string& name) {
        auto it = std::find_if(members_.begin(), members_.end(),
                              [&](const auto& member) { return member->name() == name; });
        if (it == members_.end()) { std::cout << "ERROR missing\n"; return; }
        members_.erase(it);
        std::cout << "LEFT " << name << "\n";
    }
    void send(const std::string& sender, const std::string& message) const {
        auto member = find(sender);
        if (!member) { std::cout << "ERROR missing\n"; return; }
        member->send(message);
    }
    void list() const {
        std::cout << "MEMBERS ";
        MemberIterator it(members_);
        if (!it.hasNext()) std::cout << "-";
        bool first = true;
        while (it.hasNext()) {
            if (!first) std::cout << ",";
            std::cout << it.next()->name();
            first = false;
        }
        std::cout << "\n";
    }
    void broadcast(const std::string& sender, const std::string& message) const override {
        MemberIterator it(members_);
        int delivered = 0;
        while (it.hasNext()) {
            auto member = it.next();
            if (member->name() != sender) { member->receive(sender, message); ++delivered; }
        }
        if (delivered == 0) std::cout << "NO_RECEIVERS\n";
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
