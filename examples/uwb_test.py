import serial
import threading

uwb = serial.Serial('/dev/ttyAMA0', baudrate=115200, timeout=0.5)

def read_from_uwb():
    while True:
        if uwb.in_waiting:
            #0xff 같이 이상한 데이터 읽히는 거 방지
            data = uwb.readline().decode(errors='ignore').strip()
            if data:
                print(f"UWB Response : {data}")

def write_to_uwb():
    while True:
        try:
            cmd = input(">>> ")
            if cmd.strip():
                uwb.write((cmd + '\r').encode())
        except KeyboardInterrupt:
            print("Exit")
            break

if __name__ == "__main__":
    print("--UWB Start--")
    threading.Thread(target=read_from_uwb, daemon=True).start()
    write_to_uwb()