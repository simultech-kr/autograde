#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>

class Report {
public:
    virtual ~Report() = default;
    virtual std::string render(const std::string& title, const std::string& value) const = 0;
};

// 1. Report 제품은 테마에서 생성한 제목과 값을 출력 형식으로 결합합니다.
class TextReport final : public Report {
public:
    std::string render(const std::string& title, const std::string& value) const override {
        return "title=" + title + ";value=" + value;
    }
};

class CsvReport final : public Report {
public:
    std::string render(const std::string& title, const std::string& value) const override {
        return title + "," + value;
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

// 2. Factory Method: 공통 render 절차가 호출할 제품의 종류를 결정합니다.
class TextReportCreator final : public ReportCreator {
public:
    std::unique_ptr<Report> createReport() const override {
        return std::make_unique<TextReport>();
    }
};

class CsvReportCreator final : public ReportCreator {
public:
    std::unique_ptr<Report> createReport() const override {
        return std::make_unique<CsvReport>();
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

// 3. Abstract Factory: 제목과 값 제품이 항상 같은 테마를 따릅니다.
class PlainTitle final : public TitlePart {
public:
    std::string format(const std::string& name) const override {
        return name;
    }
};

class BracketTitle final : public TitlePart {
public:
    std::string format(const std::string& name) const override {
        return "[" + name + "]";
    }
};

class PlainValue final : public ValuePart {
public:
    std::string format(int value) const override {
        return std::to_string(value);
    }
};

class BracketValue final : public ValuePart {
public:
    std::string format(int value) const override {
        return "[" + std::to_string(value) + "]";
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
        return std::make_unique<PlainTitle>();
    }
    std::unique_ptr<ValuePart> createValue() const override {
        return std::make_unique<PlainValue>();
    }
};

class BracketThemeFactory final : public ThemeFactory {
public:
    std::unique_ptr<TitlePart> createTitle() const override {
        return std::make_unique<BracketTitle>();
    }
    std::unique_ptr<ValuePart> createValue() const override {
        return std::make_unique<BracketValue>();
    }
};

// 4. Singleton: 서비스 객체의 수명과 무관하게 순번을 유지합니다.
class Sequence final {
public:
    static Sequence& instance() {
        static Sequence sequence;
        return sequence;
    }
    int next() {
        return ++count_;
    }
    int count() const {
        return count_;
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
    // 5. 선택 지점 밖에서는 추상 인터페이스를 사용해 세 패턴을 조합합니다.
    std::string make(const std::string& format, const std::string& theme,
                     const std::string& name, int value) const {
        if (format != "TEXT" && format != "CSV") {
            return "ERROR format";
        }
        if (theme != "PLAIN" && theme != "BRACKET") {
            return "ERROR theme";
        }
        if (value < 0 || value > 100) {
            return "ERROR value";
        }

        std::unique_ptr<ReportCreator> creator;
        if (format == "TEXT") {
            creator = std::make_unique<TextReportCreator>();
        } else {
            creator = std::make_unique<CsvReportCreator>();
        }
        std::unique_ptr<ThemeFactory> factory;
        if (theme == "PLAIN") {
            factory = std::make_unique<PlainThemeFactory>();
        } else {
            factory = std::make_unique<BracketThemeFactory>();
        }

        const auto title = factory->createTitle();
        const auto number = factory->createValue();
        const std::string rendered = creator->render(title->format(name), number->format(value));
        const int sequence = Sequence::instance().next();
        return std::to_string(sequence) + " " + rendered;
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
