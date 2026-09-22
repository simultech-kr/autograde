#include <iostream>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

struct Request { std::string id, category; int severity = 0; };
class Handler {
protected:
    std::unique_ptr<Handler> next_;
    virtual bool accepts(const Request& request) const = 0;
    virtual std::string result() const = 0;
public:
    virtual ~Handler() = default;
    void setNext(std::unique_ptr<Handler> next) { next_ = std::move(next); }
    std::string handle(const Request& request) const {
        if (accepts(request)) return result();
        return next_ ? next_->handle(request) : "UNHANDLED";
    }
};
class AuthHandler final : public Handler {
    bool accepts(const Request& request) const override { return request.category == "LOGIN"; }
    std::string result() const override { return "AUTH_DESK"; }
};
class NetworkHandler final : public Handler {
    bool accepts(const Request& request) const override { return request.category == "NETWORK"; }
    std::string result() const override { return "NETWORK_DESK"; }
};
class BillingHandler final : public Handler {
    bool accepts(const Request& request) const override { return request.category == "BILLING"; }
    std::string result() const override { return "BILLING_DESK"; }
};
class EscalateHandler final : public Handler {
    bool accepts(const Request& request) const override { return request.severity >= 4; }
    std::string result() const override { return "SENIOR"; }
};
std::unique_ptr<Handler> create(const std::string& key) {
    if (key == "AUTH") return std::make_unique<AuthHandler>();
    if (key == "NETWORK") return std::make_unique<NetworkHandler>();
    if (key == "BILLING") return std::make_unique<BillingHandler>();
    if (key == "ESCALATE") return std::make_unique<EscalateHandler>();
    return nullptr;
}
int main() {
    int count = 0;
    std::cin >> count;
    std::vector<std::string> keys(static_cast<std::size_t>(count));
    std::set<std::string> seen;
    bool valid = true;
    for (auto& key : keys) {
        std::cin >> key;
        if (!create(key) || !seen.insert(key).second) valid = false;
    }
    if (!valid) { std::cout << "ERROR config\n"; return 0; }
    std::unique_ptr<Handler> chain;
    for (auto it = keys.rbegin(); it != keys.rend(); ++it) {
        auto handler = create(*it);
        handler->setNext(std::move(chain));
        chain = std::move(handler);
    }
    int queries = 0;
    std::cin >> queries;
    while (queries-- > 0) {
        Request request;
        std::cin >> request.id >> request.category >> request.severity;
        std::cout << request.id << ": ";
        if (request.category != "LOGIN" && request.category != "NETWORK" &&
            request.category != "BILLING" && request.category != "OTHER") {
            std::cout << "ERROR category\n";
        } else if (request.severity < 1 || request.severity > 5) {
            std::cout << "ERROR severity\n";
        } else {
            std::cout << (chain ? chain->handle(request) : "UNHANDLED") << "\n";
        }
    }
}
