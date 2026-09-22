#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

class TextBlock;
class TaskBlock;
class BlockVisitor {
public:
    virtual ~BlockVisitor() = default;
    virtual void visit(const TextBlock&) = 0;
    virtual void visit(const TaskBlock&) = 0;
};
class Block {
public:
    virtual ~Block() = default;
    virtual std::unique_ptr<Block> clone() const = 0;
    virtual void accept(BlockVisitor&) const = 0;
};
class TextBlock final : public Block {
    std::string text_;
public:
    explicit TextBlock(std::string text) : text_(std::move(text)) {}
    const std::string& text() const { return text_; }
    std::unique_ptr<Block> clone() const override { return std::make_unique<TextBlock>(*this); }
    void accept(BlockVisitor& visitor) const override { (void)visitor; /* TODO: double dispatch */ }
};
class TaskBlock final : public Block {
    int points_;
public:
    explicit TaskBlock(int points) : points_(points) {}
    int points() const { return points_; }
    std::unique_ptr<Block> clone() const override { return std::make_unique<TaskBlock>(*this); }
    void accept(BlockVisitor& visitor) const override { (void)visitor; /* TODO: double dispatch */ }
};
class StatsVisitor final : public BlockVisitor {
public:
    int texts = 0, chars = 0, tasks = 0, points = 0;
    void visit(const TextBlock& block) override {
        (void)block; // TODO: collect text statistics
    }
    void visit(const TaskBlock& block) override {
        (void)block; // TODO: collect task statistics
    }
    std::string result() const {
        return "TEXTS " + std::to_string(texts) + " CHARS " + std::to_string(chars)
            + " TASKS " + std::to_string(tasks) + " POINTS " + std::to_string(points);
    }
};
class PrintVisitor final : public BlockVisitor {
    std::string output_;
    void append(const std::string& value) {
        if (!output_.empty()) output_ += '|';
        output_ += value;
    }
public:
    void visit(const TextBlock& block) override { (void)block; /* TODO: append text */ }
    void visit(const TaskBlock& block) override { (void)block; /* TODO: append task */ }
    std::string result() const { return output_.empty() ? "EMPTY" : output_; }
};
class Document {
    std::vector<std::unique_ptr<Block>> blocks_;
public:
    class Snapshot {
        friend class Document;
        std::vector<std::unique_ptr<Block>> blocks_;
    };
    using SnapshotPtr = std::shared_ptr<const Snapshot>;
    void text(const std::string& value) { blocks_.push_back(std::make_unique<TextBlock>(value)); }
    void task(int points) { blocks_.push_back(std::make_unique<TaskBlock>(points)); }
    void accept(BlockVisitor& visitor) const {
        (void)visitor;
        // TODO: visit every block in document order.
    }
    SnapshotPtr save() const {
        auto snapshot = std::shared_ptr<Snapshot>(new Snapshot());
        // TODO: store an independent snapshot of every block.
        return snapshot;
    }
    void restore(const Snapshot& snapshot) {
        (void)snapshot;
        // TODO: restore independent blocks, without changing the saved snapshot.
    }
};
int main() {
    int n; if (!(std::cin >> n)) return 0;
    Document document;
    std::map<std::string, Document::SnapshotPtr> snapshots;
    while (n--) {
        std::string command; std::cin >> command;
        if (command == "TEXT") {
            std::string text; std::cin >> text;
            document.text(text); std::cout << "OK\n";
        } else if (command == "TASK") {
            int points; std::cin >> points;
            if (points < 0 || points > 100) { std::cout << "ERROR points\n"; continue; }
            document.task(points); std::cout << "OK\n";
        } else if (command == "SAVE") {
            std::string id; std::cin >> id;
            if (snapshots.count(id)) { std::cout << "ERROR duplicate\n"; continue; }
            snapshots[id] = document.save(); std::cout << "OK\n";
        } else if (command == "RESTORE") {
            std::string id; std::cin >> id;
            if (!snapshots.count(id)) { std::cout << "ERROR missing\n"; continue; }
            document.restore(*snapshots.at(id)); std::cout << "OK\n";
        } else if (command == "STATS") {
            StatsVisitor visitor; document.accept(visitor);
            std::cout << visitor.result() << '\n';
        } else if (command == "PRINT") {
            PrintVisitor visitor; document.accept(visitor);
            std::cout << visitor.result() << '\n';
        }
    }
}
