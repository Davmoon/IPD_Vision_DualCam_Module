import degirum as dg
import degirum_tools
import degirum_tools.streams as dgstreams
from picamera2 import Picamera2
import cv2
import time
import requests
import sys
import os
import threading
import serial
from datetime import datetime
from gpiozero import OutputDevice
from flask import Flask, jsonify

# --- [사용자 설정] ---
inference_host_address = "@local"
zoo_url = "../models"
token = '' 

SERVER_LINK = "https://davmo.xyz/api/uploads" 
SAVE_DIR = "captures"

# 본인의 RFID 태그 ID (터미널에서 확인 후 수정)
TARGET_RFID_TAG = "E2000017570D0173277006CB"

# 하드웨어 설정
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200
RELAY_PIN = 27
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# --- [상태 관리 클래스] ---
class SystemState:
    def __init__(self):
        self.mode = "IDLE" 
        self.rfid_data = None 

state = SystemState()

# --- [1. 웹 서버 스레드] ---
app = Flask(__name__)

@app.route('/return_start', methods=['GET', 'POST'])
def start_return_process():
    if state.mode == "IDLE":
        print("\n📱 [Web] 반납 요청 수신! RFID 태깅 대기...")
        state.mode = "SCANNING" 
        return jsonify({"status": "ok", "message": "반납 모드 시작."})
    else:
        return jsonify({"status": "busy", "message": "이미 작동 중입니다."})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# --- [2. RFID 리더 스레드] ---
def rfid_reader_thread():
    print(f"📡 RFID 리더 대기 중... ({SERIAL_PORT})")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')
        
        while True:
            if state.mode == "SCANNING":
                ser.write(cmd_read)
                time.sleep(0.1)
                
                if ser.in_waiting > 0:
                    data = ser.read(ser.in_waiting)
                    hex_str = data.hex().upper()
                    
                    if len(data) > 8 and hex_str.startswith("BB"):
                        # 태그 ID 비교
                        if TARGET_RFID_TAG in hex_str:
                            print(f"✅ [RFID] 인증 성공! 카메라를 켭니다.")
                            state.rfid_data = hex_str
                            state.mode = "CAPTURING" # 촬영 모드로 전환
                        # else:
                        #     print(f"⚠️ [RFID] 미등록 태그: {hex_str}")
            
            time.sleep(0.2) 

    except Exception as e:
        print(f"❌ RFID 오류: {e}")

# --- [3. 카메라 제너레이터] ---
def picamera_generator(index):
    print(f'-- 2. {index}번 카메라 대기 모드 --')
    picam2 = None
    is_running = False

    try:
        while True:
            # CAPTURING 모드일 때만 카메라 작동
            if state.mode == "CAPTURING":
                if not is_running:
                    print("-- 3. 카메라 ON --")
                    picam2 = Picamera2(index)
                    config = picam2.create_preview_configuration(main={"size": (640, 480)})
                    picam2.configure(config)
                    picam2.start()
                    relay.on()
                    is_running = True
                    time.sleep(1.0) # 노출 안정화

                frame_rgb = picam2.capture_array()
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                yield frame_bgr

            else:
                if is_running:
                    print("-- 5. 카메라 OFF --")
                    if picam2:
                        picam2.stop()
                        picam2.close()
                        picam2 = None
                    relay.off()
                    is_running = False
                time.sleep(0.1)

    except Exception as e:
        print(f"카메라 오류: {e}")
    finally:
        if picam2: picam2.stop(); picam2.close()
        relay.off()

# --- [4. 촬영 및 전송 Gizmo (1회 촬영 로직 적용)] ---
class CaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name

    def run(self):
        for result_wrapper in self.get_input(0):
            if self._abort: break
            
            # [핵심 수정] 현재 상태가 'CAPTURING'일 때만 딱 한 번 실행!
            # 이미 IDLE로 바뀌었다면(첫 번째 사진 처리 후), 뒤따라온 사진들은 무시됨.
            if state.mode == "CAPTURING":
                print(f"\n📸 [{self.camera_name}] 찰칵! (1장 촬영 완료)")
                image = result_wrapper.data
                
                # 전송 스레드 시작
                t = threading.Thread(target=self.save_and_send_thread, 
                                     args=(image.copy(), state.rfid_data))
                t.start()

                # [중요] 상태를 즉시 'IDLE'로 변경하여 중복 촬영 방지
                print("🔄 상태 초기화: 다시 대기 모드로 돌아갑니다.")
                state.mode = "IDLE"
                state.rfid_data = None
            
            self.send_result(result_wrapper)

    def save_and_send_thread(self, image_array, rfid_data):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.camera_name}_RETURN_{timestamp}.jpg"
            
            _, img_encoded = cv2.imencode('.jpg', image_array)
            files = {'imageFile': (filename, img_encoded.tobytes(), 'image/jpeg')}
            data = {
                'camera': self.camera_name,
                'rfid': rfid_data,
                'status': 'return_complete'
            }
            
            print(f"   📡 서버 전송 중...")
            requests.post(SERVER_LINK, files=files, data=data, timeout=10.0)
            print(f"   ✅ 전송 완료!")

        except Exception as e:
            print(f"   ❌ 전송 오류: {e}")

# --- [메인 실행] ---
configurations = [
    { "model_name": "scooter_model", "source" : '0', "display_name": "cam0" },
    # { "model_name": "scooter_model", "source" : '1', "display_name": "cam1" },
]

models = [
    dg.load_model(cfg["model_name"], inference_host_address, zoo_url, token)
    for cfg in configurations
]

sources = [dgstreams.IteratorSourceGizmo(picamera_generator(int(cfg["source"]))) for cfg in configurations]
detectors = [dgstreams.AiSimpleGizmo(model) for model in models]
notifiers = [CaptureGizmo(cfg["display_name"]) for cfg in configurations]
display = dgstreams.VideoDisplayGizmo(
    [cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True
)

pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> notifier >> display[di] for di, (detector, notifier) in enumerate(zip(detectors, notifiers))),
)

threading.Thread(target=run_flask, daemon=True).start()
threading.Thread(target=rfid_reader_thread, daemon=True).start()

print("==================================================")
print(f"🚀 반납 시스템 가동! (1회 촬영 모드)")
print("==================================================")

dgstreams.Composition(*pipeline).start()