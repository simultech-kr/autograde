# 과제: 기상 관측소에 Observer Pattern 적용하기

기상 관측소(`WeatherStation`)의 측정값이 바뀔 때 등록된 화면 객체에 자동으로
전달되도록 Observer Pattern을 완성하세요.

## 구현할 부분

`src/edu/autograde/observer/WeatherStation.java`

- `registerObserver`: observer를 등록합니다.
- 같은 observer 인스턴스를 여러 번 등록해도 한 번만 알림을 보내야 합니다.
- `removeObserver`: 등록된 observer를 해제합니다. 없는 observer의 해제도 안전해야 합니다.
- `notifyObservers`: 현재 측정값을 등록된 모든 observer에게 전달합니다.
- `setMeasurements`: 새 불변 snapshot을 만든 뒤 observer들에게 알립니다.

`src/edu/autograde/observer/CurrentConditionsDisplay.java`

- `update`: 가장 최근 snapshot과 호출 횟수를 기록합니다.
- 제공된 getter의 시그니처는 변경하지 마세요.

`Observer`, `Subject`, `WeatherSnapshot`의 공개 API는 변경하지 마세요. 외부
라이브러리는 사용할 수 없으며 Java 표준 라이브러리만 사용합니다.

## 로컬 확인

JDK 11 이상에서 다음 명령으로 컴파일할 수 있습니다.

```bash
mkdir -p out
javac -encoding UTF-8 -d out $(find src -name '*.java')
```

채점에서는 고정 문자열이나 소스 텍스트를 찾지 않습니다. 새로운 observer 객체를
만들고 측정값을 여러 번 바꾸어 다음 행동을 확인합니다.

| 평가 항목 | 점수 |
| --- | ---: |
| 전체 소스 컴파일 및 공개 API 호환 | 2.0 |
| 등록된 observer 알림과 최신 측정값 전달 | 3.0 |
| 중복 등록 방지 및 observer 해제 | 2.0 |
| 복수 observer에게 독립적으로 알림 | 1.5 |
| `CurrentConditionsDisplay` 상태 갱신 | 1.5 |

