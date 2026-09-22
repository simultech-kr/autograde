#include <algorithm>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

struct Submission { std::string id; int score; };
class BoardObserver {
public:
    virtual ~BoardObserver() = default;
    virtual void onChange() = 0;
};
class CounterObserver final : public BoardObserver {
    int count_ = 0;
public:
    void onChange() override { ++count_; }
    int count() const { return count_; }
};
class ScoreIterator {
    const std::vector<Submission>& rows_;
    std::size_t position_ = 0;
    int minimum_;
    void skip() {
        while (position_ < rows_.size() && rows_[position_].score < minimum_) ++position_;
    }
public:
    ScoreIterator(const std::vector<Submission>& rows, int minimum) : rows_(rows), minimum_(minimum) { skip(); }
    bool hasNext() const { return position_ < rows_.size(); }
    const Submission& next() {
        const auto& row = rows_.at(position_++);
        skip();
        return row;
    }
};
class Board {
    std::vector<Submission> rows_;
    std::vector<std::shared_ptr<BoardObserver>> observers_;
    void notify() { for (const auto& observer : observers_) observer->onChange(); }
    auto find(const std::string& id) {
        return std::find_if(rows_.begin(), rows_.end(), [&](const Submission& row) { return row.id == id; });
    }
public:
    class Snapshot {
        friend class Board;
        std::vector<Submission> rows_;
    };
    using SnapshotPtr = std::shared_ptr<const Snapshot>;
    void subscribe(std::shared_ptr<BoardObserver> observer) { observers_.push_back(std::move(observer)); }
    void unsubscribe(const std::shared_ptr<BoardObserver>& observer) {
        observers_.erase(std::remove(observers_.begin(), observers_.end(), observer), observers_.end());
    }
    std::string add(const std::string& id, int score) {
        if (find(id) != rows_.end()) return "ERROR duplicate";
        if (score < 0 || score > 100) return "ERROR score";
        rows_.push_back({id, score}); notify(); return "OK";
    }
    std::string score(const std::string& id, int value) {
        auto row = find(id);
        if (row == rows_.end()) return "ERROR missing";
        if (value < 0 || value > 100) return "ERROR score";
        row->score = value; notify(); return "OK";
    }
    std::string remove(const std::string& id) {
        auto row = find(id);
        if (row == rows_.end()) return "ERROR missing";
        rows_.erase(row); notify(); return "OK";
    }
    ScoreIterator iterator(int minimum) const { return ScoreIterator(rows_, minimum); }
    SnapshotPtr save() const {
        auto snapshot = std::shared_ptr<Snapshot>(new Snapshot());
        snapshot->rows_ = rows_;
        return snapshot;
    }
    void restore(const Snapshot& snapshot) { rows_ = snapshot.rows_; notify(); }
};
class BoardFacade {
    Board board_;
    std::map<std::string, std::shared_ptr<CounterObserver>> monitors_;
    std::map<std::string, Board::SnapshotPtr> snapshots_;
public:
    std::string add(const std::string& id, int score) { return board_.add(id, score); }
    std::string score(const std::string& id, int score) { return board_.score(id, score); }
    std::string remove(const std::string& id) { return board_.remove(id); }
    std::string watch(const std::string& name) {
        if (monitors_.count(name)) return "ERROR duplicate";
        auto observer = std::make_shared<CounterObserver>();
        monitors_[name] = observer; board_.subscribe(observer); return "OK";
    }
    std::string unwatch(const std::string& name) {
        if (!monitors_.count(name)) return "ERROR missing";
        board_.unsubscribe(monitors_.at(name)); monitors_.erase(name); return "OK";
    }
    std::string snap(const std::string& name) {
        if (snapshots_.count(name)) return "ERROR duplicate";
        snapshots_[name] = board_.save(); return "OK";
    }
    std::string restore(const std::string& name) {
        if (!snapshots_.count(name)) return "ERROR missing";
        board_.restore(*snapshots_.at(name)); return "OK";
    }
    std::string list(int minimum) const {
        auto iterator = board_.iterator(minimum);
        std::string output;
        while (iterator.hasNext()) {
            const auto& row = iterator.next();
            if (!output.empty()) output += ' ';
            output += row.id + ":" + std::to_string(row.score);
        }
        return output.empty() ? "NONE" : output;
    }
    std::string counts() const {
        std::string output;
        for (const auto& entry : monitors_) {
            if (!output.empty()) output += ' ';
            output += entry.first + "=" + std::to_string(entry.second->count());
        }
        return output.empty() ? "NONE" : output;
    }
};
int main() {
    int n; if (!(std::cin >> n)) return 0;
    BoardFacade service;
    while (n--) {
        std::string command; std::cin >> command;
        if (command == "COUNTS") { std::cout << service.counts() << '\n'; continue; }
        if (command == "LIST") {
            int minimum; std::cin >> minimum; std::cout << service.list(minimum) << '\n'; continue;
        }
        std::string id; std::cin >> id;
        if (command == "ADD" || command == "SCORE") {
            int value; std::cin >> value;
            std::cout << (command == "ADD" ? service.add(id, value) : service.score(id, value)) << '\n';
        } else if (command == "REMOVE") std::cout << service.remove(id) << '\n';
        else if (command == "WATCH") std::cout << service.watch(id) << '\n';
        else if (command == "UNWATCH") std::cout << service.unwatch(id) << '\n';
        else if (command == "SNAP") std::cout << service.snap(id) << '\n';
        else if (command == "RESTORE") std::cout << service.restore(id) << '\n';
    }
}
