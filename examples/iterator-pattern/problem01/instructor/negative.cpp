#include <iostream>
#include <string>

// 채점기 검증용 오답: 입력 줄은 모두 소비하지만 응답은 항상 틀립니다.
int main() {
    int queries = 0;
    if (!(std::cin >> queries)) {
        return 0;
    }
    std::string line;
    std::getline(std::cin, line);
    for (int i = 0; i < queries; ++i) {
        if (!std::getline(std::cin, line)) {
            return 0;
        }
        std::cout << "ERROR negative\n";
    }
}
