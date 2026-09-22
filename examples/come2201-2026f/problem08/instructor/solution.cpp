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
    void accept(BlockVisitor& visitor) const override { visitor.visit(*this); }
};
class TaskBlock final : public Block {
    int points_;
public:
    explicit TaskBlock(int points) : points_(points) {}
    int points() const { return points_; }
    std::unique_ptr<Block> clone() const override { return std::make_unique<TaskBlock>(*this); }
    void accept(BlockVisitor& visitor) const override { visitor.visit(*this); }
};
class StatsVisitor final : public BlockVisitor {
public:
    int texts = 0, chars = 0, tasks = 0, points = 0;
    void visit(const TextBlock& block) override {
        ++texts; chars += static_cast<int>(block.text().size());
    }
    void visit(const TaskBlock& block) override {
        ++tasks; points += block.points();
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
    void visit(const TextBlock& block) override { append("TEXT:" + block.text()); }
    void visit(const TaskBlock& block) override { append("TASK:" + std::to_string(block.points())); }
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
        for (const auto& block : blocks_) block->accept(visitor);
    }
    SnapshotPtr save() const {
        auto snapshot = std::shared_ptr<Snapshot>(new Snapshot());
        for (const auto& block : blocks_) snapshot->blocks_.push_back(block->clone());
        return snapshot;
    }
    void restore(const Snapshot& snapshot) {
        std::vector<std::unique_ptr<Block>> restored;
        for (const auto& block : snapshot.blocks_) restored.push_back(block->clone());
        blocks_ = std::move(restored);
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
