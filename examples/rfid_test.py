import serial
import time
import sys

# --- 설정 ---
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

def calc_crc(payload):
    return sum(payload) % 256

def send_cmd(ser, payload):
    cmd = [0xBB, 0x00] + payload + [calc_crc(payload), 0x7E]
    ser.write(bytearray(cmd))
    time.sleep(0.1)
    if ser.in_waiting > 0:
        return ser.read(ser.in_waiting)
    return None

def set_power(ser, dbm):
    val = int(dbm * 100)
    # CMD: B6 (Set Power)
    resp = send_cmd(ser, [0xB6, 0x00, 0x02, (val >> 8) & 0xFF, val & 0xFF])
    if resp and resp[5] == 0x00:
        print(f"✅ 파워 설정 성공: {dbm} dBm")
    else:
        print(f"❌ 파워 설정 실패")

def set_region(ser, region_code):
    # CMD: 07 (Set Region)
    # 01:China1, 02:China2, 03:Europe, 04:USA, 06:Korea
    names = {1:"China1", 2:"China2", 3:"EU", 4:"USA", 6:"Korea"}
    print(f"🔄 지역 변경 중... -> {names.get(region_code, 'Unknown')}")
    resp = send_cmd(ser, [0x07, 0x00, 0x01, region_code])
    if resp and resp[5] == 0x00:
        print(f"✅ 지역 설정 완료")
    else:
        print(f"❌ 지역 설정 실패")

def rssi_test_loop(ser):
    print("\n📡 RSSI 신호 강도 테스트 시작 (Ctrl+C로 종료)")
    print("------------------------------------------------")
    cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')
    
    try:
        while True:
            ser.write(cmd_read)
            time.sleep(0.05) # 측정 주기
            
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                hex_str = data.hex().upper()
                
                # 데이터 패킷 분석
                if len(data) > 8 and hex_str.startswith("BB02"):
                    # YRM100 프로토콜에서 RSSI는 5번째 바이트 (인덱스 5)
                    rssi = data[5] 
                    tag_id = hex_str[16:40] # EPC ID 부분
                    
                    # 시각화 (Bar graph)
                    # RSSI는 보통 0(약함) ~ 128(강함) 사이 값
                    bar_len = int(rssi / 2)
                    bar = "█" * bar_len
                    
                    print(f"ID: ...{tag_id[-6:]} | RSSI: {rssi:03d} | {bar}")
            
    except KeyboardInterrupt:
        print("\n🛑 테스트 종료")

def main():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    except Exception as e:
        print(f"포트 열기 실패: {e}")
        return

    while True:
        print("\n=== RFID 정밀 진단 도구 ===")
        print("1. 상태 확인 (파워/지역)")
        print("2. 지역 변경: USA (902-928MHz) - 추천!")
        print("3. 지역 변경: Korea (917-923MHz)")
        print("4. 파워 변경: 19 dBm (안전)")
        print("5. 파워 변경: 23 dBm (보통)")
        print("6. 파워 변경: 26 dBm (최대-위험)")
        print("7. RSSI 신호 측정 모드 (실시간)")
        print("q. 종료")
        
        sel = input("선택 >> ")
        
        if sel == '1':
            # 파워 조회
            resp = send_cmd(ser, [0xB7, 0x00, 0x00])
            if resp: 
                pwr = (resp[5] << 8 | resp[6]) / 100
                print(f"Current Power: {pwr} dBm")
            # 지역 조회
            resp = send_cmd(ser, [0x06, 0x00, 0x00])
            if resp:
                reg = resp[5]
                print(f"Current Region Code: {reg:02X}")
                
        elif sel == '2': set_region(ser, 0x04) # USA
        elif sel == '3': set_region(ser, 0x06) # Korea
        elif sel == '4': set_power(ser, 19.0)
        elif sel == '5': set_power(ser, 23.0)
        elif sel == '6': set_power(ser, 26.0)
        elif sel == '7': rssi_test_loop(ser)
        elif sel == 'q': break
        
    ser.close()

if __name__ == "__main__":
    main()