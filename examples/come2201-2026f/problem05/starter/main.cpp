#include <iostream>
#include <memory>
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
        (void)request;
        return "UNHANDLED"; // TODO: handle here or delegate to next_.
    }
};
// TODO: implement AuthHandler, NetworkHandler, BillingHandler, EscalateHandler.
// TODO: construct a linked chain in the exact input order.
int main() {
    int count = 0;
    std::cin >> count;
    std::vector<std::string> keys(static_cast<std::size_t>(count));
    for (auto& key : keys) std::cin >> key;
    int queries = 0;
    std::cin >> queries;
    while (queries-- > 0) {
        Request request;
        std::cin >> request.id >> request.category >> request.severity;
        // TODO: validate and route this request, then print the required line.
    }
}
