import serial
import time

SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

def calc_crc(payload):
    return sum(payload) % 256

def send_cmd(ser, payload):
    cmd = [0xBB, 0x00] + payload + [calc_crc(payload), 0x7E]
    ser.write(bytearray(cmd))
    time.sleep(0.2) # 충분한 대기 시간
    # 응답을 읽어서 버퍼를 비워줌 (화면엔 출력 안 함)
    if ser.in_waiting:
        ser.read(ser.in_waiting)

def boost_rfid():
    print(f"🚀 RFID 성능 최대화 설정 시작 ({SERIAL_PORT})")
    
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1.0)
        
        # 1. 일단 멈춤
        send_cmd(ser, [0x28, 0x00, 0x00])
        ser.reset_input_buffer()
        print("   -> 대기 상태 전환")

        # 2. 지역 설정: USA (0x04)
        # 한국 태그라도 USA 설정이 대역폭이 넓어 인식이 훨씬 잘 됩니다.
        print("1. 주파수 대역 확장 (Korea -> USA)...")
        send_cmd(ser, [0x07, 0x00, 0x01, 0x04])
        
        # 3. 파워 설정: 26dBm (Max Power)
        # 2600 -> 0x0A28
        # 아까는 20dBm(07D0)이었습니다.
        print("2. 송신 파워 최대 출력 (20dBm -> 26dBm)...")
        send_cmd(ser, [0xB6, 0x00, 0x02, 0x0A, 0x28])
        
        ser.close()
        print("\n✅ 설정 전송 완료!")
        print("   이제 dual_cam3.py를 실행해서 거리가 늘어났는지 확인하세요.")
        print("   (목표 거리: 1m ~ 2.5m)")

    except Exception as e:
        print(f"오류: {e}")

if __name__ == "__main__":
    boost_rfid()