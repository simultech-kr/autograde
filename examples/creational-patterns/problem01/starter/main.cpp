#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

class Report {
public:
    virtual ~Report() = default;
    virtual std::string render(const std::string& title, const std::string& value) const = 0;
};

// TODO 1: TEXT는 title=<title>;value=<value>, CSV는 <title>,<value>를 반환합니다.
class TextReport final : public Report {
public:
    std::string render(const std::string&, const std::string&) const override {
        return "";
    }
};

class CsvReport final : public Report {
public:
    std::string render(const std::string&, const std::string&) const override {
        return "";
    }
};

class ReportCreator {
public:
    virtual ~ReportCreator() = default;
    virtual std::unique_ptr<Report> createReport() const = 0;

    // 고정 공통 절차: 이 메서드는 수정하지 않습니다.
    std::string render(const std::string& title, const std::string& value) const {
        auto report = createReport();
        if (!report) {
            throw std::logic_error("TODO: createReport");
        }
        return report->render(title, value);
    }
};

// TODO 2: 각 Factory Method가 자신의 Report 제품을 생성하도록 구현합니다.
class TextReportCreator final : public ReportCreator {
public:
    std::unique_ptr<Report> createReport() const override {
        return nullptr;
    }
};

class CsvReportCreator final : public ReportCreator {
public:
    std::unique_ptr<Report> createReport() const override {
        return nullptr;
    }
};

class TitlePart {
public:
    virtual ~TitlePart() = default;
    virtual std::string format(const std::string& name) const = 0;
};

class ValuePart {
public:
    virtual ~ValuePart() = default;
    virtual std::string format(int value) const = 0;
};

// TODO 3: PLAIN/BRACKET 제품과 같은 테마의 제품 쌍을 생성하는 팩터리를 구현합니다.
class PlainTitle final : public TitlePart {
public:
    std::string format(const std::string&) const override {
        return "";
    }
};

class BracketTitle final : public TitlePart {
public:
    std::string format(const std::string&) const override {
        return "";
    }
};

class PlainValue final : public ValuePart {
public:
    std::string format(int) const override {
        return "";
    }
};

class BracketValue final : public ValuePart {
public:
    std::string format(int) const override {
        return "";
    }
};

class ThemeFactory {
public:
    virtual ~ThemeFactory() = default;
    virtual std::unique_ptr<TitlePart> createTitle() const = 0;
    virtual std::unique_ptr<ValuePart> createValue() const = 0;
};

class PlainThemeFactory final : public ThemeFactory {
public:
    std::unique_ptr<TitlePart> createTitle() const override {
        return nullptr;
    }
    std::unique_ptr<ValuePart> createValue() const override {
        return nullptr;
    }
};

class BracketThemeFactory final : public ThemeFactory {
public:
    std::unique_ptr<TitlePart> createTitle() const override {
        return nullptr;
    }
    std::unique_ptr<ValuePart> createValue() const override {
        return nullptr;
    }
};

// TODO 4: 단일 스레드에서 모든 서비스가 공유하는 Singleton 순번을 구현합니다.
class Sequence final {
public:
    static Sequence& instance() {
        throw std::logic_error("TODO: Sequence::instance");
    }
    int next() {
        return 0;
    }
    int count() const {
        return 0;
    }

    Sequence(const Sequence&) = delete;
    Sequence& operator=(const Sequence&) = delete;
    Sequence(Sequence&&) = delete;
    Sequence& operator=(Sequence&&) = delete;

private:
    Sequence() = default;
    int count_ = 0;
};

class ReportService {
public:
    // TODO 5: format -> theme -> value 순으로 검증하고 추상 인터페이스로 조립합니다.
    // 오류는 ERROR format/theme/value, 유효한 값은 0..100입니다.
    // Creator의 공통 render가 완료된 뒤에만 Sequence::next()를 호출합니다.
    std::string make(const std::string&, const std::string&, const std::string&, int) const {
        throw std::logic_error("TODO: ReportService::make");
    }
};

// 고정 입출력 절차: main은 수정하지 않습니다.
int main() {
    int queries = 0;
    if (!(std::cin >> queries)) {
        return 0;
    }
    for (int i = 0; i < queries; ++i) {
        std::string command;
        if (!(std::cin >> command)) {
            return 0;
        }
        try {
            if (command == "PRINT") {
                std::string format, theme, name;
                int value = 0;
                if (!(std::cin >> format >> theme >> name >> value)) {
                    return 0;
                }
                const ReportService service;
                const std::string output = service.make(format, theme, name, value);
                std::cout << output << '\n';
            } else if (command == "COUNT") {
                const int count = Sequence::instance().count();
                std::cout << "COUNT " << count << '\n';
            } else if (command == "SAME") {
                const bool same = &Sequence::instance() == &Sequence::instance();
                std::cout << "SAME " << (same ? "yes" : "no") << '\n';
            }
        } catch (const std::logic_error&) {
            std::cout << "ERROR incomplete\n";
        }
    }
}
