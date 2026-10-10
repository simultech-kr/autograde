#include <cstddef>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// 제공된 외부 장치 API입니다. 두 클래스의 인터페이스와 구현은 수정하지 않습니다.
class LegacyLamp final {
public:
    void writeStep(int step) { step_ = step; }
    int readStep() const { return step_; }
private:
    int step_ = 0;
};

class LegacySpeaker final {
public:
    void changeVolume(int tick) { tick_ = tick; }
    int volume() const { return tick_; }
private:
    int tick_ = 0;
};

class LevelDevice {
public:
    virtual ~LevelDevice() = default;
    virtual void setLevel(int percent) = 0;
    virtual int level() const = 0;
};

// 1. Adapter는 실제 장치 단위와 percent 인터페이스를 변환합니다.
class LampAdapter final : public LevelDevice {
public:
    explicit LampAdapter(LegacyLamp& lamp) : lamp_(lamp) {}
    void setLevel(int percent) override { lamp_.writeStep(percent / 10); }
    int level() const override { return lamp_.readStep() * 10; }
private:
    LegacyLamp& lamp_;
};

class SpeakerAdapter final : public LevelDevice {
public:
    explicit SpeakerAdapter(LegacySpeaker& speaker) : speaker_(speaker) {}
    void setLevel(int percent) override { speaker_.changeVolume(percent / 5); }
    int level() const override { return speaker_.volume() * 5; }
private:
    LegacySpeaker& speaker_;
};

class Command {
public:
    virtual ~Command() = default;
    virtual void execute() = 0;
    virtual void undo() = 0;
};

// 2. 상태 변경 요청과 실행 직전 상태를 명령 객체에 캡슐화합니다.
class SetLevelCommand final : public Command {
public:
    SetLevelCommand(LevelDevice& device, int target)
        : device_(device), target_(target) {}
    void execute() override {
        previous_ = device_.level();
        device_.setLevel(target_);
    }
    void undo() override { device_.setLevel(previous_); }
private:
    LevelDevice& device_;
    int target_;
    int previous_ = 0;
};

// 3. 복합 명령 하나로 두 장치 상태를 변경하고 역순으로 복구합니다.
class SceneCommand final : public Command {
public:
    SceneCommand(LevelDevice& light, LevelDevice& sound, int lightLevel, int soundLevel)
        : light_(light, lightLevel), sound_(sound, soundLevel) {}
    void execute() override {
        light_.execute();
        sound_.execute();
    }
    void undo() override {
        sound_.undo();
        light_.undo();
    }
private:
    SetLevelCommand light_;
    SetLevelCommand sound_;
};

// 4. 실행에 성공한 명령의 소유권을 보관하며 마지막 명령부터 복구합니다.
class CommandHistory final {
public:
    void run(std::unique_ptr<Command> command) {
        if (!command) {
            throw std::logic_error("command must not be null");
        }
        command->execute();
        history_.push_back(std::move(command));
    }
    bool undoLast() {
        if (history_.empty()) {
            return false;
        }
        history_.back()->undo();
        history_.pop_back();
        return true;
    }
    std::size_t size() const { return history_.size(); }
private:
    std::vector<std::unique_ptr<Command>> history_;
};

class RoomFacade final {
public:
    RoomFacade() : light_(lamp_), sound_(speaker_) {}
    RoomFacade(const RoomFacade&) = delete;
    RoomFacade& operator=(const RoomFacade&) = delete;
    RoomFacade(RoomFacade&&) = delete;
    RoomFacade& operator=(RoomFacade&&) = delete;

    // 5. Facade가 검증과 명령 생성을 조정하되 장치 변경은 명령에 위임합니다.
    std::string set(const std::string& device, int percent) {
        if (device != "LIGHT" && device != "SOUND") {
            return "ERROR device";
        }
        if (percent < 0 || percent > 100 || percent % 10 != 0) {
            return "ERROR level";
        }
        LevelDevice& selected = device == "LIGHT"
            ? static_cast<LevelDevice&>(light_) : static_cast<LevelDevice&>(sound_);
        history_.run(std::make_unique<SetLevelCommand>(selected, percent));
        return "OK SET";
    }
    std::string scene(const std::string& name) {
        int lightLevel = 0;
        int soundLevel = 0;
        if (name == "STUDY") {
            lightLevel = 80;
            soundLevel = 20;
        } else if (name == "MOVIE") {
            lightLevel = 20;
            soundLevel = 60;
        } else if (name != "OFF") {
            return "ERROR scene";
        }
        history_.run(std::make_unique<SceneCommand>(light_, sound_, lightLevel, soundLevel));
        return "OK SCENE";
    }

    // 아래 조회/복구 진입점과 멤버 순서는 제공 코드입니다.
    std::string undo() { return history_.undoLast() ? "OK UNDO" : "EMPTY"; }
    std::string status() const {
        return "STATE light=" + std::to_string(light_.level())
            + " sound=" + std::to_string(sound_.level());
    }
    std::string raw() const {
        return "RAW lamp=" + std::to_string(lamp_.readStep())
            + " speaker=" + std::to_string(speaker_.volume());
    }
    std::size_t historySize() const { return history_.size(); }

private:
    // 참조되는 장치 -> Adapter -> CommandHistory 순서로 생성되어야 합니다.
    // 소멸은 역순입니다. 장치/Adapter보다 명령들이 먼저 소멸합니다.
    LegacyLamp lamp_;
    LegacySpeaker speaker_;
    LampAdapter light_;
    SpeakerAdapter sound_;
    CommandHistory history_;
};

// 고정 입출력 절차: main은 수정하지 않습니다.
int main() {
    int queries = 0;
    if (!(std::cin >> queries)) {
        return 0;
    }
    RoomFacade room;
    for (int i = 0; i < queries; ++i) {
        std::string command;
        if (!(std::cin >> command)) {
            return 0;
        }
        try {
            std::string output;
            if (command == "SET") {
                std::string device;
                int percent = 0;
                if (!(std::cin >> device >> percent)) {
                    return 0;
                }
                output = room.set(device, percent);
            } else if (command == "SCENE") {
                std::string name;
                if (!(std::cin >> name)) {
                    return 0;
                }
                output = room.scene(name);
            } else if (command == "UNDO") {
                output = room.undo();
            } else if (command == "STATUS") {
                output = room.status();
            } else if (command == "RAW") {
                output = room.raw();
            } else if (command == "HISTORY") {
                output = "HISTORY " + std::to_string(room.historySize());
            }
            std::cout << output << '\n';
        } catch (const std::logic_error&) {
            std::cout << "ERROR incomplete\n";
        }
    }
}
