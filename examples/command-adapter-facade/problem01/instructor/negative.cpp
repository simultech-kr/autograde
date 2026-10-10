#include <iostream>
#include <string>

// 채점기 검증용 오답: 문법상 정상 입력을 소비하지만 모든 응답이 틀립니다.
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
        if (command == "SET") {
            std::string device;
            int percent = 0;
            if (!(std::cin >> device >> percent)) {
                return 0;
            }
        } else if (command == "SCENE") {
            std::string name;
            if (!(std::cin >> name)) {
                return 0;
            }
        }
        std::cout << "ERROR negative\n";
    }
}
