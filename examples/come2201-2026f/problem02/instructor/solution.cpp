#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

class Component {
public:
    virtual ~Component() = default;
    virtual int price() const = 0;
    virtual bool contains(const Component* target) const { return this == target; }
};
class Part final : public Component {
    int amount_;
public:
    explicit Part(int amount) : amount_(amount) {}
    int price() const override { return amount_; }
};
class PartCreator {
public:
    virtual ~PartCreator() = default;
    virtual std::shared_ptr<Component> create() const = 0;
};
class CPUCreator final : public PartCreator {
public:
    std::shared_ptr<Component> create() const override { return std::make_shared<Part>(100); }
};
class RAMCreator final : public PartCreator {
public:
    std::shared_ptr<Component> create() const override { return std::make_shared<Part>(40); }
};
class DISKCreator final : public PartCreator {
public:
    std::shared_ptr<Component> create() const override { return std::make_shared<Part>(60); }
};
class Group final : public Component {
    std::vector<std::shared_ptr<Component>> children_;
public:
    int price() const override {
        int total = 0;
        for (const auto& child : children_) total += child->price();
        return total;
    }
    bool contains(const Component* target) const override {
        if (this == target) return true;
        for (const auto& child : children_) if (child->contains(target)) return true;
        return false;
    }
    void add(std::shared_ptr<Component> child) { children_.push_back(std::move(child)); }
};
class QuoteFacade {
    std::map<std::string, std::shared_ptr<Component>> items_;
    std::map<std::string, std::string> parents_;
public:
    void part(const std::string& id, const std::string& type) {
        if (items_.count(id)) { std::cout << "ERROR duplicate\n"; return; }
        std::unique_ptr<PartCreator> creator;
        if (type == "CPU") creator = std::make_unique<CPUCreator>();
        else if (type == "RAM") creator = std::make_unique<RAMCreator>();
        else if (type == "DISK") creator = std::make_unique<DISKCreator>();
        else { std::cout << "ERROR type\n"; return; }
        items_[id] = creator->create();
        std::cout << "CREATED " << id << "\n";
    }
    void group(const std::string& id) {
        if (items_.count(id)) { std::cout << "ERROR duplicate\n"; return; }
        items_[id] = std::make_shared<Group>();
        std::cout << "CREATED " << id << "\n";
    }
    void link(const std::string& parent, const std::string& child) {
        if (!items_.count(parent) || !items_.count(child)) { std::cout << "ERROR missing\n"; return; }
        auto group = std::dynamic_pointer_cast<Group>(items_.at(parent));
        if (!group) { std::cout << "ERROR parent\n"; return; }
        if (items_.at(child)->contains(group.get())) { std::cout << "ERROR cycle\n"; return; }
        if (parents_.count(child)) { std::cout << "ERROR linked\n"; return; }
        group->add(items_.at(child));
        parents_[child] = parent;
        std::cout << "LINKED " << parent << " " << child << "\n";
    }
    void total(const std::string& id) const {
        auto item = items_.find(id);
        if (item == items_.end()) { std::cout << "ERROR missing\n"; return; }
        std::cout << "TOTAL " << item->second->price() << "\n";
    }
};
int main() {
    int count = 0;
    std::cin >> count;
    QuoteFacade quote;
    while (count-- > 0) {
        std::string command, first, second;
        std::cin >> command >> first;
        if (command == "PART") { std::cin >> second; quote.part(first, second); }
        else if (command == "GROUP") quote.group(first);
        else if (command == "LINK") { std::cin >> second; quote.link(first, second); }
        else if (command == "TOTAL") quote.total(first);
    }
}
