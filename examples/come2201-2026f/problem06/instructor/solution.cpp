#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

class Report {
    std::string title_;
    int pages_;
    std::vector<std::string> sections_;
public:
    Report(std::string title, int pages) : title_(std::move(title)), pages_(pages) {}
    std::unique_ptr<Report> clone() const {
        return std::make_unique<Report>(*this);
    }
    void title(const std::string& value) { title_ = value; }
    void add(const std::string& section) { sections_.push_back(section); }
    std::string summary(const std::string& id) const {
        std::ostringstream out;
        out << id << '|' << title_ << '|' << pages_ << '|';
        if (sections_.empty()) out << '-';
        for (std::size_t i = 0; i < sections_.size(); ++i) {
            if (i) out << ',';
            out << sections_[i];
        }
        return out.str();
    }
};

class ReportBuilder {
    std::string title_;
    int pages_ = 0;
public:
    ReportBuilder& withTitle(const std::string& title) { title_ = title; return *this; }
    ReportBuilder& withPages(int pages) { pages_ = pages; return *this; }
    std::unique_ptr<Report> build() const {
        if (title_.empty() || pages_ < 1 || pages_ > 500) return nullptr;
        return std::make_unique<Report>(title_, pages_);
    }
};

int main() {
    int n;
    if (!(std::cin >> n)) return 0;
    std::map<std::string, std::unique_ptr<Report>> reports;
    while (n--) {
        std::string command, id;
        std::cin >> command >> id;
        if (command == "BUILD") {
            std::string title; int pages;
            std::cin >> title >> pages;
            if (reports.count(id)) { std::cout << "ERROR duplicate\n"; continue; }
            auto report = ReportBuilder().withTitle(title).withPages(pages).build();
            if (!report) { std::cout << "ERROR pages\n"; continue; }
            reports[id] = std::move(report);
            std::cout << "OK\n";
        } else if (command == "CLONE") {
            std::string target; std::cin >> target;
            if (!reports.count(id)) { std::cout << "ERROR missing\n"; continue; }
            if (reports.count(target)) { std::cout << "ERROR duplicate\n"; continue; }
            reports[target] = reports.at(id)->clone();
            if (!reports[target]) { reports.erase(target); std::cout << "ERROR clone\n"; continue; }
            std::cout << "OK\n";
        } else if (command == "TITLE" || command == "ADD") {
            std::string value; std::cin >> value;
            if (!reports.count(id)) { std::cout << "ERROR missing\n"; continue; }
            if (command == "TITLE") reports.at(id)->title(value);
            else reports.at(id)->add(value);
            std::cout << "OK\n";
        } else if (command == "SHOW") {
            if (!reports.count(id)) { std::cout << "ERROR missing\n"; continue; }
            std::cout << reports.at(id)->summary(id) << '\n';
        }
    }
}
