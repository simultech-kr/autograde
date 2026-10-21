#include <cstddef>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

struct Submission {
    std::string id;
    int score;
};

class SubmissionIterator {
public:
    virtual ~SubmissionIterator() = default;
    virtual bool hasNext() const = 0;
    virtual std::optional<Submission> next() = 0;
    virtual void reset() = 0;
};

// 각 Iterator가 생성 시점의 기록을 값으로 소유합니다.
// snapshot_은 이후 원본 목록의 ADD/CLEAR와 관계없이 유지됩니다.
class ScoreIterator final : public SubmissionIterator {
public:
    ScoreIterator(std::vector<Submission> snapshot, int minScore)
        : snapshot_(std::move(snapshot)), minScore_(minScore) {
        advanceToMatch();
    }

    // 구현 2: 다음 기록의 존재 여부만 확인합니다. 위치를 변경하지 않습니다.
    bool hasNext() const override {
        return position_ < snapshot_.size();
    }

    // 구현 3: 종료 상태면 nullopt, 아니면 현재 기록을 값으로 반환합니다.
    // 반환할 기록 저장 -> 위치 증가 -> 다음 조건 일치 위치 탐색 순서입니다.
    std::optional<Submission> next() override {
        if (!hasNext()) {
            return std::nullopt;
        }
        const Submission record = snapshot_[position_];
        ++position_;
        advanceToMatch();
        return record;
    }

    // 구현 4: 같은 snapshot_과 minScore_로 처음부터 다시 순회합니다.
    void reset() override {
        position_ = 0;
        advanceToMatch();
    }

private:
    // 구현 1: position_이 끝 또는 score >= minScore_인 위치가 될 때까지 이동합니다.
    // 배열 접근 전에 범위를 확인하고, 조건에 맞는 기록은 건너뛰지 않습니다.
    void advanceToMatch() {
        while (position_ < snapshot_.size() && snapshot_[position_].score < minScore_) {
            ++position_;
        }
    }

    const std::vector<Submission> snapshot_;
    const int minScore_;
    std::size_t position_ = 0;
};

class SubmissionCollection {
public:
    virtual ~SubmissionCollection() = default;
    virtual std::unique_ptr<SubmissionIterator> createIterator(int minScore) const = 0;
};

class SubmissionLog final : public SubmissionCollection {
public:
    // 아래 등록/초기화/크기 조회는 제공 코드입니다.
    std::string add(const std::string& id, int score) {
        if (score < 0 || score > 100) {
            return "ERROR score";
        }
        for (const Submission& record : records_) {
            if (record.id == id) {
                return "ERROR duplicate";
            }
        }
        records_.push_back(Submission{id, score});
        return "OK ADD";
    }

    void clear() { records_.clear(); }
    std::size_t size() const { return records_.size(); }

    // 구현 5: records_를 복사해 소유하는 ScoreIterator를 생성합니다.
    // minScore는 호출자가 검증했습니다. 추상 Iterator의 unique_ptr를 반환합니다.
    std::unique_ptr<SubmissionIterator> createIterator(int minScore) const override {
        return std::make_unique<ScoreIterator>(records_, minScore);
    }

private:
    std::vector<Submission> records_;
};

// 제공된 Client: 내부 vector를 직접 읽지 않고 Iterator 인터페이스로 순회합니다.
class LogApp final {
public:
    std::string add(const std::string& id, int score) { return log_.add(id, score); }

    std::string clear() {
        log_.clear();
        return "OK CLEAR";
    }

    std::string size() const { return "SIZE " + std::to_string(log_.size()); }

    std::string open(const std::string& slot, int minScore) {
        const int index = slotIndex(slot);
        if (index < 0) {
            return "ERROR slot";
        }
        if (minScore < 0 || minScore > 100) {
            return "ERROR score";
        }
        const SubmissionCollection& collection = log_;
        auto replacement = collection.createIterator(minScore);
        iterators_[index] = std::move(replacement);
        return "OK OPEN";
    }

    std::string has(const std::string& slot) {
        std::string error;
        SubmissionIterator* iterator = findIterator(slot, error);
        if (iterator == nullptr) {
            return error;
        }
        return iterator->hasNext() ? "HAS 1" : "HAS 0";
    }

    std::string next(const std::string& slot) {
        std::string error;
        SubmissionIterator* iterator = findIterator(slot, error);
        if (iterator == nullptr) {
            return error;
        }
        const std::optional<Submission> record = iterator->next();
        if (!record) {
            return "END";
        }
        return "ITEM " + record->id + " " + std::to_string(record->score);
    }

    std::string reset(const std::string& slot) {
        std::string error;
        SubmissionIterator* iterator = findIterator(slot, error);
        if (iterator == nullptr) {
            return error;
        }
        iterator->reset();
        return "OK RESET";
    }

private:
    static int slotIndex(const std::string& slot) {
        if (slot == "A") {
            return 0;
        }
        if (slot == "B") {
            return 1;
        }
        return -1;
    }

    SubmissionIterator* findIterator(const std::string& slot, std::string& error) {
        const int index = slotIndex(slot);
        if (index < 0) {
            error = "ERROR slot";
            return nullptr;
        }
        if (!iterators_[index]) {
            error = "ERROR unopened";
            return nullptr;
        }
        return iterators_[index].get();
    }

    SubmissionLog log_;
    std::unique_ptr<SubmissionIterator> iterators_[2];
};

// 고정 입출력 절차입니다. main과 LogApp, 제공된 검증 코드는 수정하지 않습니다.
int main() {
    int queries = 0;
    if (!(std::cin >> queries)) {
        return 0;
    }
    LogApp app;
    for (int i = 0; i < queries; ++i) {
        std::string command;
        if (!(std::cin >> command)) {
            return 0;
        }
        try {
            std::string output;
            if (command == "ADD") {
                std::string id;
                int score = 0;
                if (!(std::cin >> id >> score)) {
                    return 0;
                }
                output = app.add(id, score);
            } else if (command == "CLEAR") {
                output = app.clear();
            } else if (command == "SIZE") {
                output = app.size();
            } else if (command == "OPEN") {
                std::string slot;
                int minScore = 0;
                if (!(std::cin >> slot >> minScore)) {
                    return 0;
                }
                output = app.open(slot, minScore);
            } else if (command == "HAS" || command == "NEXT" || command == "RESET") {
                std::string slot;
                if (!(std::cin >> slot)) {
                    return 0;
                }
                if (command == "HAS") {
                    output = app.has(slot);
                } else if (command == "NEXT") {
                    output = app.next(slot);
                } else {
                    output = app.reset(slot);
                }
            }
            std::cout << output << '\n';
        } catch (const std::logic_error&) {
            std::cout << "ERROR incomplete\n";
        }
    }
}
