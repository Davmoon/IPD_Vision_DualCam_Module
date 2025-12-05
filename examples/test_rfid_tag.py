import serial
import time

# [중요] 아까 성공한 포트로 설정
PORT = '/dev/ttyAMA0'
BAUD = 115200

# '한 번 읽기 (Single Poll)' 명령어
cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')

print("--- 🏷️ RFID 태그 인식 테스트 ---")
print("태그를 리더기 1m 이내로 가져오세요... (Ctrl+C로 종료)")

try:
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    
    while True:
        # 명령 전송
        ser.write(cmd_read)
        time.sleep(0.1)
        
        # 데이터 수신
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting)
            hex_str = data.hex().upper()
            
            # 응답이 있고, 길이가 충분하면(태그 데이터 포함) 출력
            # (BB 02 ... 로 시작하는 응답이 태그 정보입니다)
            if hex_str.startswith("BB") and len(data) > 8:
                print(f"✅ 태그 감지! 데이터: {hex_str}")
        
        # 0.2초 간격으로 반복
        time.sleep(0.2)

except KeyboardInterrupt:
    print("\n테스트 종료")
    if 'ser' in locals() and ser.is_open:
        ser.close()
except Exception as e:
    print(f"오류: {e}")