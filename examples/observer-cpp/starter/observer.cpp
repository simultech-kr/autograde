#include "observer.hpp"

namespace observer_lab {

void Subject::attach(Observer* observer) {
    // TODO: nullptr와 중복 등록을 제외하고 Observer를 등록하세요.
    (void)observer;
}

void Subject::detach(Observer* observer) {
    // TODO: 등록되어 있다면 안전하게 해제하세요.
    (void)observer;
}

void Subject::setState(int new_state) {
    // TODO: 상태를 먼저 변경한 뒤 Observer에게 알리세요.
    state_ = new_state;
}

int Subject::state() const noexcept {
    return state_;
}

void Subject::notify() {
    // TODO: 알림 시작 시점의 Observer 목록을 기준으로 등록 순서대로 호출하세요.
}

}  // namespace observer_lab
