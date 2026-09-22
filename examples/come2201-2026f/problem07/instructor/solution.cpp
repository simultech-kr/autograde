#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>

class MarkerStyle {
    std::string color_;
public:
    explicit MarkerStyle(std::string color) : color_(std::move(color)) {}
    const std::string& color() const { return color_; }
};
class StyleFactory {
    std::map<std::string, std::shared_ptr<const MarkerStyle>> cache_;
public:
    std::shared_ptr<const MarkerStyle> get(const std::string& color) {
        auto& style = cache_[color];
        if (!style) style = std::make_shared<const MarkerStyle>(color);
        return style;
    }
    std::size_t count() const { return cache_.size(); }
};
class Renderer {
public:
    virtual ~Renderer() = default;
    virtual std::string render(const std::string& id, const MarkerStyle& style, int x, int y) const = 0;
};
class TextRenderer final : public Renderer {
public:
    std::string render(const std::string& id, const MarkerStyle& style, int x, int y) const override {
        return "TEXT " + id + " " + style.color() + " " + std::to_string(x) + "," + std::to_string(y);
    }
};
class CompactRenderer final : public Renderer {
public:
    std::string render(const std::string& id, const MarkerStyle& style, int x, int y) const override {
        return "COMPACT[" + id + ";" + style.color() + ";" + std::to_string(x) + ";" + std::to_string(y) + "]";
    }
};
class MapMarker {
    std::string id_;
    std::shared_ptr<const MarkerStyle> style_;
    int x_, y_;
    std::shared_ptr<const Renderer> renderer_;
public:
    MapMarker(std::string id, std::shared_ptr<const MarkerStyle> style, int x, int y)
        : id_(std::move(id)), style_(std::move(style)), x_(x), y_(y) {}
    void move(int x, int y) { x_ = x; y_ = y; }
    void setRenderer(std::shared_ptr<const Renderer> renderer) { renderer_ = std::move(renderer); }
    std::string draw() const {
        return renderer_ ? renderer_->render(id_, *style_, x_, y_) : "ERROR renderer";
    }
};
bool validPosition(int x, int y) {
    return x >= -1000 && x <= 1000 && y >= -1000 && y <= 1000;
}
int main() {
    int n; if (!(std::cin >> n)) return 0;
    StyleFactory styles;
    std::map<std::string, std::unique_ptr<MapMarker>> markers;
    const auto text = std::make_shared<TextRenderer>();
    const auto compact = std::make_shared<CompactRenderer>();
    while (n--) {
        std::string command; std::cin >> command;
        if (command == "COUNT") { std::cout << "STYLES " << styles.count() << '\n'; continue; }
        std::string id; std::cin >> id;
        if (command == "ADD") {
            std::string color; int x, y; std::cin >> color >> x >> y;
            if (markers.count(id)) { std::cout << "ERROR duplicate\n"; continue; }
            if (!validPosition(x, y)) { std::cout << "ERROR position\n"; continue; }
            markers[id] = std::make_unique<MapMarker>(id, styles.get(color), x, y);
            std::cout << "OK\n";
        } else if (command == "MOVE") {
            int x, y; std::cin >> x >> y;
            if (!markers.count(id)) { std::cout << "ERROR missing\n"; continue; }
            if (!validPosition(x, y)) { std::cout << "ERROR position\n"; continue; }
            markers.at(id)->move(x, y);
            std::cout << "OK\n";
        } else if (command == "DRAW") {
            std::string mode; std::cin >> mode;
            if (!markers.count(id)) { std::cout << "ERROR missing\n"; continue; }
            if (mode == "TEXT") markers.at(id)->setRenderer(text);
            else if (mode == "COMPACT") markers.at(id)->setRenderer(compact);
            else { std::cout << "ERROR renderer\n"; continue; }
            std::cout << markers.at(id)->draw() << '\n';
        }
    }
}
