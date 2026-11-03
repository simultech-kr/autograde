#include <array>
#include <cstddef>
#include <iostream>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

struct Request {
    int id;
    std::string sender;
    std::string kind;
    int priority;
};

class Handler {
public:
    Handler(std::string name, std::string role)
        : name_(std::move(name)), role_(std::move(role)) {}
    virtual ~Handler() = default;
    void setNext(std::unique_ptr<Handler> next) { next_ = std::move(next); }

    std::optional<std::string> handle(const Request& request,
                                      std::vector<std::string>& trace) const {
        // TODO 1: 방문 시 name_을 trace에 추가합니다. accepts가 참이면 role_을
        // 반환하고 즉시 종료합니다. 아니면 next_로 전달하고, 끝이면 nullopt입니다.
        (void)request;
        (void)trace;
        return std::nullopt;
    }

protected:
    virtual bool accepts(const Request& request) const = 0;
    std::string name_;
    std::string role_;
    std::unique_ptr<Handler> next_;
};

class PriorityHandler final : public Handler {
public:
    PriorityHandler() : Handler("PRIORITY", "PROF") {}
protected:
    bool accepts(const Request& request) const override {
        // TODO 2: priority가 3이고 kind가 OTHER가 아닌 요청만 처리합니다.
        (void)request;
        return false;
    }
};

class KindHandler final : public Handler {
public:
    KindHandler(std::string kind, std::string role)
        : Handler(kind, std::move(role)), kind_(std::move(kind)) {}
protected:
    bool accepts(const Request& request) const override {
        // TODO 3: 요청의 kind가 이 처리자의 kind_와 같은지 검사합니다.
        (void)request;
        return false;
    }
private:
    std::string kind_;
};

// Provided factory: routing policy belongs to the handlers and buildChain.
std::unique_ptr<Handler> makeHandler(const std::string& name) {
    if (name == "PRIORITY") return std::make_unique<PriorityHandler>();
    if (name == "CODE") return std::make_unique<KindHandler>("CODE", "TUTOR");
    if (name == "ENV") return std::make_unique<KindHandler>("ENV", "TECH");
    if (name == "GRADE") return std::make_unique<KindHandler>("GRADE", "PROF");
    throw std::invalid_argument("unknown handler");
}

std::unique_ptr<Handler> buildChain(const std::string& mode) {
    // TODO 4: makeHandler와 setNext로 unique_ptr 소유권을 연결합니다.
    // PRIORITY: PRIORITY -> CODE -> ENV -> GRADE
    // DOMAIN: CODE -> ENV -> GRADE -> PRIORITY, EMPTY: nullptr
    (void)mode;
    return nullptr;
}

class ILabMediator {
public:
    virtual ~ILabMediator() = default;
    virtual std::string send(const Request& request) = 0;
};

class StudentTerminal final {
public:
    StudentTerminal(std::string name, ILabMediator& mediator)
        : name_(std::move(name)), mediator_(mediator) {}
    std::string send(int id, const std::string& kind, int priority) {
        // TODO 5: name_을 sender로 넣은 Request를 mediator_.send에 전달합니다.
        // 학생은 다른 학생이나 직원의 주소/이름을 조회하지 않습니다.
        (void)name_;
        (void)mediator_;
        (void)kind;
        (void)priority;
        return "UNHANDLED " + std::to_string(id);
    }
private:
    std::string name_;
    ILabMediator& mediator_;
};

// Each recipient owns its own inbox; it has no references to other terminals.
class StaffTerminal final {
public:
    bool online() const { return online_; }
    void setOnline(bool online) { online_ = online; }
    void deliver(const Request& request) { inbox_.push_back(request); }
    const std::vector<Request>& inbox() const { return inbox_; }
private:
    bool online_ = true;
    std::vector<Request> inbox_;
};

class LabMediator final : public ILabMediator {
public:
    LabMediator() : head_(buildChain("PRIORITY")) {}
    LabMediator(const LabMediator&) = delete;
    LabMediator& operator=(const LabMediator&) = delete;
    LabMediator(LabMediator&&) = delete;
    LabMediator& operator=(LabMediator&&) = delete;

    std::string send(const Request& request) override {
        // TODO 6: trace 초기화 -> 성공한 ID 중복 확인 -> 체인 선택 -> 온라인 검사.
        // 첫 선택 직원이 오프라인이면 OFFLINE이며 다른 처리자로 넘기지 않습니다.
        // 성공 시에만 선택 직원에게 1회 배달하고 ID와 해당 학생의 성공 수를 기록합니다.
        // 반환: SENT id role / OFFLINE id role / UNHANDLED id / DUPLICATE id
        return "UNHANDLED " + std::to_string(request.id);
    }

    // Provided commands: replacing the chain preserves all delivery state.
    void setMode(const std::string& mode) {
        head_ = buildChain(mode);
        lastTrace_.clear();
    }
    void setOnline(const std::string& role, bool online) {
        staff_[roleIndex(role)].setOnline(online);
    }
    void printTrace(std::ostream& out) const {
        out << "TRACE ";
        if (lastTrace_.empty()) out << '-';
        for (std::size_t i = 0; i < lastTrace_.size(); ++i) {
            if (i != 0) out << '>';
            out << lastTrace_[i];
        }
        out << '\n';
    }
    void printInbox(const std::string& role, std::ostream& out) const {
        const auto& inbox = staff_[roleIndex(role)].inbox();
        out << "INBOX " << role << ' ' << inbox.size() << '\n';
        for (const Request& request : inbox) {
            out << "ITEM " << request.id << ' ' << request.sender << ' '
                << request.kind << ' ' << request.priority << '\n';
        }
    }
    void printStatus(std::ostream& out) const {
        out << "STATUS A " << senderCounts_[0] << " B " << senderCounts_[1]
            << " TUTOR " << staff_[0].inbox().size()
            << " TECH " << staff_[1].inbox().size()
            << " PROF " << staff_[2].inbox().size() << '\n';
    }

private:
    static std::size_t roleIndex(const std::string& role) {
        if (role == "TUTOR") return 0;
        if (role == "TECH") return 1;
        return 2; // Validated role is PROF.
    }
    std::array<StaffTerminal, 3> staff_;
    std::set<int> acceptedIds_;
    std::array<std::size_t, 2> senderCounts_{};
    std::vector<std::string> lastTrace_;
    std::unique_ptr<Handler> head_;
};

// Provided input validation: finish parsing the entire line before any mutation.
bool atEnd(std::istringstream& input) {
    input >> std::ws;
    return input.eof();
}
bool isRole(const std::string& role) {
    return role == "TUTOR" || role == "TECH" || role == "PROF";
}
bool isKind(const std::string& kind) {
    return kind == "CODE" || kind == "ENV" || kind == "GRADE" || kind == "OTHER";
}
bool executeLine(const std::string& line, LabMediator& mediator,
                 StudentTerminal& studentA, StudentTerminal& studentB) {
    std::istringstream input(line);
    std::string command;
    if (!(input >> command)) return false;
    if (command == "SEND") {
        int id = 0, priority = 0;
        std::string sender, kind;
        if (!(input >> id >> sender >> kind >> priority) || !atEnd(input)
            || id < 1 || id > 10000 || (sender != "A" && sender != "B")
            || !isKind(kind) || priority < 1 || priority > 3) return false;
        auto& student = sender == "A" ? studentA : studentB;
        std::cout << student.send(id, kind, priority) << '\n';
    } else if (command == "MODE") {
        std::string mode;
        if (!(input >> mode) || !atEnd(input)
            || (mode != "PRIORITY" && mode != "DOMAIN" && mode != "EMPTY")) return false;
        mediator.setMode(mode);
        std::cout << "OK MODE\n";
    } else if (command == "ONLINE") {
        std::string role;
        int online = 0;
        if (!(input >> role >> online) || !atEnd(input)
            || !isRole(role) || (online != 0 && online != 1)) return false;
        mediator.setOnline(role, online == 1);
        std::cout << "OK ONLINE\n";
    } else if (command == "INBOX") {
        std::string role;
        if (!(input >> role) || !atEnd(input) || !isRole(role)) return false;
        mediator.printInbox(role, std::cout);
    } else if (command == "TRACE") {
        if (!atEnd(input)) return false;
        mediator.printTrace(std::cout);
    } else if (command == "STATUS") {
        if (!atEnd(input)) return false;
        mediator.printStatus(std::cout);
    } else {
        return false;
    }
    return true;
}

int main() {
    std::string line;
    if (!std::getline(std::cin, line)) return 1;
    std::istringstream header(line);
    int commands = 0;
    if (!(header >> commands) || !atEnd(header) || commands < 1 || commands > 200) return 1;
    LabMediator mediator; // Constructed first, destroyed after both students.
    StudentTerminal studentA("A", mediator), studentB("B", mediator);
    for (int i = 0; i < commands; ++i) {
        if (!std::getline(std::cin, line)) {
            std::cout << "ERROR input\n";
            return 1;
        }
        if (!executeLine(line, mediator, studentA, studentB)) std::cout << "ERROR input\n";
    }
}
