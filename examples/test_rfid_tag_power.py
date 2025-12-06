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
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [사용자 설정] ---
inference_host_address = "@local"
zoo_url = "../models"
token = '' 

SERVER_LINK = "https://davmo.xyz/api/uploads" 
SAVE_DIR = "captures"

# [중요] 타겟 태그 ID
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
        # 상태 목록:
        # "IDLE": 아무것도 안 함 (평소)
        # "WAIT_FOR_TAG": 웹 요청 받음 -> 올바른 태그 기다리는 중
        # "CAPTURING": 태그 확인됨 -> 카메라 켜고 촬영 중
        self.mode = "IDLE" 
        self.rfid_data = None 

state = SystemState()

# --- [1. 웹 서버 스레드] ---
app = Flask(__name__)

@app.route('/return_start', methods=['GET', 'POST'])
def start_return_process():
    if state.mode == "IDLE":
        print("\n📱 [Web] 반납 요청 수신! 태그 인증 대기 중...")
        state.mode = "WAIT_FOR_TAG" # 이제부터 태그가 맞는지 검사 시작
        return jsonify({"status": "ok", "message": "태그를 리더기에 대주세요."})
    elif state.mode == "WAIT_FOR_TAG":
        return jsonify({"status": "waiting", "message": "이미 태그를 기다리고 있습니다."})
    else:
        return jsonify({"status": "busy", "message": "시스템이 작동 중입니다."})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# --- [2. RFID 리더 스레드 (상시 가동)] ---
def rfid_reader_thread():
    print(f"📡 RFID 리더 상시 가동 중... ({SERIAL_PORT})")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.05)
        cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')
        
        while True:
            # [변경] 조건문 없이 항상 읽습니다.
            ser.write(cmd_read)
            time.sleep(0.05) # 반응 속도 빠름
            
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                hex_str = data.hex().upper()
                
                if len(data) > 8 and hex_str.startswith("BB"):
                    # 태그가 읽혔음!
                    
                    # [로직] 웹에서 요청이 왔을 때만("WAIT_FOR_TAG") 반응
                    if state.mode == "WAIT_FOR_TAG":
                        # ID 추출 (16~40번째 글자)
                        # (혹시 추출이 불안하면 전체 문자열 검색으로 변경 가능)
                        try:
                            # 만약 추출이 어렵다면 아래 줄 주석하고 if TARGET in hex_str: 사용
                            # current_epc = hex_str[16:40] 
                            
                            if TARGET_RFID_TAG in hex_str:
                                print(f"\n✅ [RFID] 인증 성공! ({TARGET_RFID_TAG})")
                                print("   --> 카메라 부팅 시작!")
                                state.rfid_data = TARGET_RFID_TAG
                                state.mode = "CAPTURING" # 카메라 깨우기
                            else:
                                # 다른 태그가 읽힘 (로그가 너무 많으면 주석 처리)
                                # print(f"⚠️ [RFID] 미등록 태그 감지")
                                pass
                        except: pass
            
            time.sleep(0.05)

    except Exception as e:
        print(f"❌ RFID 오류: {e}")

# --- [3. 카메라 제너레이터] ---
def picamera_generator(index):
    print(f'-- 2. {index}번 카메라 대기 모드 --')
    picam2 = None
    is_running = False

    try:
        while True:
            # CAPTURING 모드가 되면 카메라 켜기
            if state.mode == "CAPTURING":
                if not is_running:
                    print("-- 3. 카메라 ON --")
                    picam2 = Picamera2(index)
                    config = picam2.create_preview_configuration(main={"size": (640, 480)})
                    picam2.configure(config)
                    picam2.start()
                    relay.on()
                    is_running = True
                    time.sleep(1.0) 

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

# --- [4. 촬영 및 전송 Gizmo] ---
class CaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name

    def run(self):
        for result_wrapper in self.get_input(0):
            if self._abort: break
            
            # 카메라가 켜졌고(CAPTURING), 이미지가 들어옴 -> 바로 촬영
            if state.mode == "CAPTURING":
                print(f"\n📸 [{self.camera_name}] 찰칵! 전송 시작...")
                image = result_wrapper.data
                
                t = threading.Thread(target=self.save_and_send_thread, 
                                     args=(image.copy(), state.rfid_data))
                t.start()

                print("🔄 상태 초기화: 다시 대기합니다.")
                state.mode = "IDLE" # 초기화
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
            
            # 타임아웃 15초
            response = requests.post(SERVER_LINK, files=files, data=data, timeout=15.0, verify=False)
            
            if response.status_code == 200:
                print(f"   ✅ 전송 성공!")
            else:
                print(f"   ⚠️ 전송 실패 (Code: {response.status_code})")

        except Exception as e:
            print(f"   ❌ 전송 오류: {e}")

# --- [메인 실행] ---
configurations = [
    { "model_name": "scooter_model", "source" : '0', "display_name": "cam0" },
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
print(f"🚀 시스템 시작! (RFID 상시 가동 중)")
print("==================================================")

dgstreams.Composition(*pipeline).start()