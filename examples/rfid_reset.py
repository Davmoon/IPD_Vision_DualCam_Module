import serial
import time

SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

def send_cmd(ser, hex_cmd):
    cmd = bytes.fromhex(hex_cmd)
    print(f"보내는 명령: {cmd.hex().upper()}")
    ser.write(cmd)
    time.sleep(0.5)
    if ser.in_waiting:
        resp = ser.read(ser.in_waiting)
        print(f"받은 응답: {resp.hex().upper()}")
        return resp
    print("응답 없음")
    return None

def rescue_rfid():
    print(f"🚑 RFID 모듈 긴급 구조 작업 시작 ({SERIAL_PORT})")
    
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1.0)
        
        # 1. 소프트웨어 리셋 (Reset)
        # 칩 내부를 완전히 재부팅합니다.
        print("\n1. 소프트웨어 리셋 명령 전송...")
        # CMD: 0C (Reset)
        send_cmd(ser, 'BB 00 0C 00 00 0C 7E')
        
        print("   ⏳ 리셋 중... 3초 대기...")
        time.sleep(3.0) # 부팅 시간 확보
        ser.reset_input_buffer()

        # 2. 파워 조회 (살아났는지 확인)
        print("\n2. 모듈 생존 확인 (파워 조회)...")
        resp = send_cmd(ser, 'BB 00 B7 00 00 B7 7E')
        
        if resp and resp.startswith(b'\xBB\x01\xB7'):
            print("   ✅ 모듈이 응답합니다!")
        else:
            print("   ❌ 모듈이 응답하지 않습니다. 하드웨어 고장 가능성 있음.")

        # 3. 안전 모드 설정 (China2: 840-845MHz)
        # Korea/USA 설정이 꼬였을 때, 다른 대역으로 변경 충격을 줘서 푸는 방법입니다.
        print("\n3. 안전 대역(China2)으로 변경 시도...")
        # CMD: 07, Data: 02 (China2)
        resp = send_cmd(ser, 'BB 00 07 00 01 02 0A 7E')
        
        if resp and b'FF' not in resp:
            print("   ✅ China2 설정 성공! (메모리 락이 풀렸습니다)")
            
            # 4. 원래 목표인 Korea(06) 또는 USA(04)로 복귀
            print("\n4. 목표 대역(Korea)으로 재설정...")
            # Korea(06) 설정
            send_cmd(ser, 'BB 00 07 00 01 06 0E 7E')
            
            # 최종 확인
            print("\n5. 최종 확인 (지역 조회)")
            send_cmd(ser, 'BB 00 06 00 00 06 7E')
            
        else:
            print("   ❌ 설정 실패 (여전히 오류 17 발생)")
            print("   👉 하드웨어적인 전원 차단(케이블 뽑기)을 1분 이상 유지 후 다시 시도하세요.")

        ser.close()

    except Exception as e:
        print(f"오류: {e}")

if __name__ == "__main__":
    rescue_rfid()