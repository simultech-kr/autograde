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

// TODO 1: percent와 장치 단위를 양방향으로 변환합니다.
// 별도 percent 캐시를 만들지 말고 참조 중인 실제 장치에서 현재 값을 읽습니다.
class LampAdapter final : public LevelDevice {
public:
    explicit LampAdapter(LegacyLamp& lamp) : lamp_(lamp) {}
    void setLevel(int) override {
        throw std::logic_error("TODO 1: LampAdapter::setLevel");
    }
    int level() const override {
        throw std::logic_error("TODO 1: LampAdapter::level");
    }
private:
    LegacyLamp& lamp_;
};

class SpeakerAdapter final : public LevelDevice {
public:
    explicit SpeakerAdapter(LegacySpeaker& speaker) : speaker_(speaker) {}
    void setLevel(int) override {
        throw std::logic_error("TODO 1: SpeakerAdapter::setLevel");
    }
    int level() const override {
        throw std::logic_error("TODO 1: SpeakerAdapter::level");
    }
private:
    LegacySpeaker& speaker_;
};

class Command {
public:
    virtual ~Command() = default;
    virtual void execute() = 0;
    virtual void undo() = 0;
};

// TODO 2: execute 시작 시 이전 값을 저장한 뒤 target_을 적용합니다.
// 생성 시점과 실행 시점은 다를 수 있습니다. undo는 저장한 값으로 복구합니다.
class SetLevelCommand final : public Command {
public:
    SetLevelCommand(LevelDevice& device, int target)
        : device_(device), target_(target) {}
    void execute() override {
        throw std::logic_error("TODO 2: SetLevelCommand::execute");
    }
    void undo() override {
        throw std::logic_error("TODO 2: SetLevelCommand::undo");
    }
private:
    LevelDevice& device_;
    int target_;
    int previous_ = 0;
};

// TODO 3: 두 명령을 하나의 명령으로 묶습니다.
// 실행 순서는 light_ -> sound_, 복구 순서는 그 반대입니다.
class SceneCommand final : public Command {
public:
    SceneCommand(LevelDevice& light, LevelDevice& sound, int lightLevel, int soundLevel)
        : light_(light, lightLevel), sound_(sound, soundLevel) {}
    void execute() override {
        throw std::logic_error("TODO 3: SceneCommand::execute");
    }
    void undo() override {
        throw std::logic_error("TODO 3: SceneCommand::undo");
    }
private:
    SetLevelCommand light_;
    SetLevelCommand sound_;
};

// TODO 4: 명령 실행 성공 후 unique_ptr 소유권을 history_로 이동합니다.
// undoLast는 마지막 명령 복구 후 제거하고 true, 비어 있으면 false를 반환합니다.
class CommandHistory final {
public:
    void run(std::unique_ptr<Command>) {
        throw std::logic_error("TODO 4: CommandHistory::run");
    }
    bool undoLast() {
        throw std::logic_error("TODO 4: CommandHistory::undoLast");
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

    // TODO 5: set과 scene에서 입력 검증 -> 명령 생성 -> history_.run 순으로 처리합니다.
    // set: LIGHT/SOUND 검사 우선, 그다음 0..100 및 10의 배수 검사.
    // 오류는 ERROR device / ERROR level. 성공은 OK SET.
    std::string set(const std::string&, int) {
        throw std::logic_error("TODO 5: RoomFacade::set");
    }
    // STUDY=(80,20), MOVIE=(20,60), OFF=(0,0). 오류는 ERROR scene.
    // 성공은 OK SCENE. SceneCommand 하나를 등록하며 값이 같아도 생략하지 않습니다.
    std::string scene(const std::string&) {
        throw std::logic_error("TODO 5: RoomFacade::scene");
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
