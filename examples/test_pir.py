from gpiozero import MotionSensor
from time import sleep

# GPIO 17번 핀에 연결 (데이터 핀)
# 만약 다른 핀에 연결했다면 숫자를 변경하세요.
pir = MotionSensor(17)

print("--- PIR 센서 테스트 시작 ---")
print("센서 안정화를 위해 2초간 대기합니다...")
sleep(2)
print("준비 완료! 센서 앞에서 손을 흔들어보세요.")
print("(종료하려면 Ctrl+C를 누르세요)")

try:
    while True:
        # pir.value가 1(True)이면 감지된 것, 0(False)이면 없는 것
        if pir.value:
            print("🏃 움직임 감지됨! (Motion Detected)")
        else:
            print("... 감지 안됨 (No Motion)")
        
        # 0.5초마다 상태 확인
        sleep(0.5)

except KeyboardInterrupt:
    print("\n테스트를 종료합니다.")