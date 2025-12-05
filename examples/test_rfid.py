import serial
import time

# 라즈베리파이 시리얼 포트 (안 되면 '/dev/serial0' 또는 '/dev/ttyS0'로 변경)
PORT = '/dev/ttyAMA0'
BAUD = 115200

print(f"--- 📡 RFID 모듈 연결 테스트 ({PORT}) ---")

try:
    # 1. 시리얼 포트 열기
    ser = serial.Serial(PORT, BAUD, timeout=1.0)
    
    # 2. '하드웨어 버전 조회' 명령어 전송
    # (YRM100 공통 프로토콜: 헤더 BB 00, 명령 03, ...)
    cmd = bytes.fromhex('BB 00 03 00 01 00 04 7E')
    
    print(f"📤 보냄: {cmd.hex().upper()}")
    ser.write(cmd)
    time.sleep(0.2) # 모듈이 대답할 시간 주기
    
    # 3. 응답 확인
    if ser.in_waiting > 0:
        response = ser.read(ser.in_waiting)
        hex_res = response.hex().upper()
        print(f"📥 받음: {hex_res}")
        
        if hex_res.startswith("BB"):
            print("\n🎉 [성공] 모듈이 정상적으로 연결되었습니다!")
        else:
            print("\n⚠️ [주의] 데이터는 오지만 내용이 이상합니다. (Baudrate 또는 노이즈 문제)")
    else:
        print("\n❌ [실패] 모듈이 아무 대답이 없습니다.")
        print("   1. RX/TX 핀이 반대로 꽂혔는지 확인하세요.")
        print("   2. 5V 전원이 제대로 들어갔는지 확인하세요.")

except Exception as e:
    print(f"\n❌ 오류 발생: {e}")
    print(f"   --> {PORT} 포트가 없거나 권한이 없습니다.")

finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()