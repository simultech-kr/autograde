#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

class MenuItem {
public:
    virtual ~MenuItem() = default;
    virtual long long price() const = 0;
    virtual long long units() const = 0;
};
using ItemPtr = std::shared_ptr<const MenuItem>;
class Drink final : public MenuItem {
    int basePrice_;
public:
    explicit Drink(int price) : basePrice_(price) {}
    long long price() const override { return basePrice_; }
    long long units() const override { return 1; }
};
class DrinkFactory {
public:
    ItemPtr create(const std::string& kind) const {
        if (kind == "tea") return std::make_shared<Drink>(3000);
        if (kind == "coffee") return std::make_shared<Drink>(4000);
        return nullptr;
    }
};
class ExtraDecorator final : public MenuItem {
    ItemPtr wrapped_;
    int extraPrice_;
public:
    ExtraDecorator(ItemPtr wrapped, int extraPrice) : wrapped_(std::move(wrapped)), extraPrice_(extraPrice) {}
    long long price() const override { return wrapped_->price() + extraPrice_; }
    long long units() const override { return wrapped_->units(); }
};
class DrinkPack final : public MenuItem {
    std::vector<ItemPtr> children_;
public:
    explicit DrinkPack(std::vector<ItemPtr> children) : children_(std::move(children)) {}
    long long price() const override {
        long long total = 0;
        for (const auto& child : children_) total += child->price();
        return total;
    }
    long long units() const override {
        long long total = 0;
        for (const auto& child : children_) total += child->units();
        return total;
    }
};
class OrderFacade {
    DrinkFactory factory_;
    std::map<std::string, ItemPtr> menu_;
public:
    std::string drink(const std::string& id, const std::string& kind) {
        if (menu_.count(id)) return "ERROR duplicate";
        auto item = factory_.create(kind);
        if (!item) return "ERROR kind";
        menu_[id] = std::move(item); return "OK";
    }
    std::string extra(const std::string& id, const std::string& source, const std::string& kind) {
        if (menu_.count(id)) return "ERROR duplicate";
        if (!menu_.count(source)) return "ERROR missing";
        int amount = 0;
        if (kind == "milk") amount = 500;
        else if (kind == "shot") amount = 1000;
        else return "ERROR extra";
        menu_[id] = std::make_shared<ExtraDecorator>(menu_.at(source), amount);
        return "OK";
    }
    std::string pack(const std::string& id, const std::vector<std::string>& members, int count) {
        if (menu_.count(id)) return "ERROR duplicate";
        if (count < 1 || count > 10) return "ERROR count";
        std::vector<ItemPtr> children;
        for (const auto& name : members) {
            if (!menu_.count(name)) return "ERROR missing";
            children.push_back(menu_.at(name));
        }
        menu_[id] = std::make_shared<DrinkPack>(std::move(children)); return "OK";
    }
    std::string quote(const std::string& id) const {
        if (!menu_.count(id)) return "ERROR missing";
        const auto& item = menu_.at(id);
        return "PRICE " + std::to_string(item->price()) + " ITEMS " + std::to_string(item->units());
    }
    std::string order(const std::string& id, int count) const {
        if (!menu_.count(id)) return "ERROR missing";
        if (count < 1 || count > 100) return "ERROR count";
        return "TOTAL " + std::to_string(menu_.at(id)->price() * count);
    }
};
int main() {
    int n; if (!(std::cin >> n)) return 0;
    OrderFacade shop;
    while (n--) {
        std::string command, id; std::cin >> command >> id;
        if (command == "DRINK") {
            std::string kind; std::cin >> kind; std::cout << shop.drink(id, kind) << '\n';
        } else if (command == "EXTRA") {
            std::string source, kind; std::cin >> source >> kind;
            std::cout << shop.extra(id, source, kind) << '\n';
        } else if (command == "PACK") {
            int count; std::cin >> count;
            std::vector<std::string> members;
            for (int i = 0; i < count; ++i) { std::string name; std::cin >> name; members.push_back(name); }
            std::cout << shop.pack(id, members, count) << '\n';
        } else if (command == "QUOTE") {
            std::cout << shop.quote(id) << '\n';
        } else if (command == "ORDER") {
            int count; std::cin >> count; std::cout << shop.order(id, count) << '\n';
        }
    }
}
