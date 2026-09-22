#include <iostream>
#include <memory>
#include <string>
#include <vector>

class Component {
public:
    virtual ~Component() = default;
    virtual int price() const = 0;
};
class PartCreator {
public:
    virtual ~PartCreator() = default;
    virtual std::shared_ptr<Component> create() const = 0;
};
// TODO: Implement CPUCreator, RAMCreator, DISKCreator and leaf Parts.
class Group : public Component {
public:
    int price() const override { return 0; } // TODO: recursively sum children.
private:
    std::vector<std::shared_ptr<Component>> children_;
};
// TODO: Add registry, parent tracking, factory selection, validation to the facade.
class QuoteFacade {
public:
    void part(const std::string& id, const std::string& type) { (void)id; (void)type; }
    void group(const std::string& id) { (void)id; }
    void link(const std::string& parent, const std::string& child) { (void)parent; (void)child; }
    void total(const std::string& id) const { (void)id; }
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
