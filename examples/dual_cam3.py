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
from gpiozero import MotionSensor, OutputDevice
from flask import Flask, jsonify
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [사용자 설정] ---
inference_host_address = "@local"
zoo_url = "../models"
token = '' 

SERVER_LINK = "https://davmo.xyz/api/uploads" 
SAVE_DIR = "captures"

# [중요] 본인의 RFID 태그 ID
TARGET_RFID_TAG = "E2000017570D0173277006CB" 

# 하드웨어 핀
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200
PIR_PIN = 17
RELAY_PIN = 27

pir = MotionSensor(PIR_PIN)
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# --- [전역 상태 및 릴레이 관리] ---
class SystemState:
    def __init__(self):
        self.mode = "IDLE" 
        self.rfid_data = None
        self.finished_count = 0 
        self.lock = threading.Lock()
        self.relay_off_time = 0.0

state = SystemState()

def extend_relay(seconds):
    target_time = time.time() + seconds
    if target_time > state.relay_off_time:
        state.relay_off_time = target_time

# [스레드 1] 릴레이 관리자
def relay_manager_thread():
    while True:
        if time.time() < state.relay_off_time:
            if not relay.value: relay.on()
        else:
            if relay.value: relay.off()
        time.sleep(0.1)

# [스레드 2] PIR 센서 감시자 (보안등 기능)
def pir_monitor_thread():
    print(f"🏃 PIR 감시 시작 ({PIR_PIN}번)")
    while True:
        if pir.value:
            extend_relay(30.0)
        time.sleep(0.2)

# --- [스레드 3] 웹 서버 ---
app = Flask(__name__)

@app.route('/return_start', methods=['GET', 'POST'])
def start_return_process():
    if state.mode == "IDLE":
        print("\n📱 [Web] 반납 요청 수신! 태그 인증 대기 중...")
        state.mode = "WAIT_FOR_TAG"
        return jsonify({"status": "ok", "message": "Please use RFID tag."})
    else:
        return jsonify({"status": "busy", "message": "System Running"})

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# --- [스레드 4] RFID 리더 ---
def rfid_reader_thread():
    print(f"📡 RFID 리더 대기 중... ({SERIAL_PORT})")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.05)
        cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')
        
        while True:
            ser.write(cmd_read)
            time.sleep(0.05)
            
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                hex_str = data.hex().upper()
                
                if len(data) > 8 and hex_str.startswith("BB"):
                    # 반납 대기 모드일 때만 처리
                    if state.mode == "WAIT_FOR_TAG":
                        if TARGET_RFID_TAG in hex_str:
                            print(f"\n✅ [RFID] 인증 성공! 카메라를 켭니다.")
                            
                            with state.lock:
                                state.finished_count = 0 # 카운트 초기화
                                state.rfid_data = TARGET_RFID_TAG
                                state.mode = "CAPTURING" # 카메라 켜기
            
            time.sleep(0.05)

    except Exception as e:
        print(f"❌ RFID 오류: {e}")

# --- [5. 카메라 제너레이터] ---
def picamera_generator(index):
    print(f'-- {index}번 카메라 준비 완료 --')
    picam2 = None
    is_running = False

    try:
        while True:
            # CAPTURING 모드일 때 카메라 켜기
            if state.mode == "CAPTURING":
                if not is_running:
                    print(f"📷 [{index}번] 카메라 부팅... AI 감지 시작")
                    try:
                        picam2 = Picamera2(index)
                        config = picam2.create_preview_configuration(main={"size": (640, 480)})
                        picam2.configure(config)
                        picam2.start()
                        
                        extend_relay(30.0) # 조명 30초 확보
                        
                        is_running = True
                        time.sleep(1.0 + (index * 0.5)) 
                    except Exception as e:
                        print(f"❌ [{index}번] 실패: {e}")
                        yield None
                        continue

                frame_rgb = picam2.capture_array()
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                yield frame_bgr

            else:
                if is_running:
                    print(f"💤 [{index}번] 카메라 종료")
                    if picam2:
                        picam2.stop()
                        picam2.close()
                        picam2 = None
                    is_running = False
                time.sleep(0.1)

    except Exception as e:
        print(f"제너레이터 오류({index}): {e}")
    finally:
        if picam2: picam2.stop(); picam2.close()

# --- [6. 스마트 촬영 Gizmo (AI 조건 적용)] ---
class SmartCaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.has_shot = False # 촬영 완료 깃발

    def run(self):
        for result_wrapper in self.get_input(0):
            if self._abort: break
            
            # 모드가 바뀌면 깃발 초기화
            if state.mode != "CAPTURING":
                self.has_shot = False

            # [핵심 로직] 촬영 모드이고, 아직 안 찍었으면 AI 분석 시작
            if state.mode == "CAPTURING" and not self.has_shot:
                
                # 1. AI 결과 찾기 (안전한 파싱)
                inf_result = None
                if hasattr(result_wrapper.data, 'result'):
                    inf_result = result_wrapper.data
                else:
                    try:
                        for item in result_wrapper.meta._meta_list:
                            if hasattr(item, 'results'):
                                inf_result = item; break
                    except: pass

                # 2. 결과 분석 (스쿠터 >= 80%)
                if inf_result and inf_result.results:
                    for obj in inf_result.results:
                        label = obj.get('label', '')
                        score = obj.get('score', 0) * 100

                        # [조건 충족!]
                        if 'scooter' in label and score >= 80.0:
                            print(f"\n🎯 [{self.camera_name}] 스쿠터 확인됨! ({score:.1f}%) -> 찰칵!")
                            
                            # 사진 전송
                            t = threading.Thread(target=self.save_and_send_thread, 
                                                 args=(result_wrapper.data.copy(), state.rfid_data))
                            t.start()

                            self.has_shot = True # 완료 표시
                            
                            # 2대 모두 찍었는지 확인
                            with state.lock:
                                state.finished_count += 1
                                print(f"   --> 진행률: {state.finished_count} / {len(configurations)}")
                                
                                if state.finished_count >= len(configurations):
                                    print("🔄 미션 완료! 대기 모드로 복귀.")
                                    state.mode = "IDLE"
                                    state.rfid_data = None
                            
                            break # 루프 탈출 (중복 전송 방지)
            
            self.send_result(result_wrapper)

    def save_and_send_thread(self, image_array, rfid_data):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.camera_name}_RETURN_{timestamp}.jpg"
            
            _, img_encoded = cv2.imencode('.jpg', image_array)
            files = {'imageFile': (filename, img_encoded.tobytes(), 'image/jpeg')}
            data = {'camera': self.camera_name, 'rfid': rfid_data, 'status': 'return_complete'}
            
            requests.post(SERVER_LINK, files=files, data=data, timeout=15.0, verify=False)
            print(f"   ✅ [{self.camera_name}] 전송 완료!")

        except Exception as e:
            print(f"   ❌ [{self.camera_name}] 전송 오류: {e}")

# --- [메인 실행] ---
configurations = [
    { "model_name": "scooter_model", "source" : '0', "display_name": "cam0" },
    { "model_name": "scooter_model", "source" : '1', "display_name": "cam1" },
]

models = [
    dg.load_model(cfg["model_name"], inference_host_address, zoo_url, token)
    for cfg in configurations
]

sources = [dgstreams.IteratorSourceGizmo(picamera_generator(int(cfg["source"]))) for cfg in configurations]
detectors = [dgstreams.AiSimpleGizmo(model) for model in models]
notifiers = [SmartCaptureGizmo(cfg["display_name"]) for cfg in configurations] # 이름 변경됨
display = dgstreams.VideoDisplayGizmo(
    [cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True
)

pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> notifier >> display[di] for di, (detector, notifier) in enumerate(zip(detectors, notifiers))),
)

threading.Thread(target=run_flask, daemon=True).start()
threading.Thread(target=rfid_reader_thread, daemon=True).start()
threading.Thread(target=relay_manager_thread, daemon=True).start()
threading.Thread(target=pir_monitor_thread, daemon=True).start()

print("==================================================")
print(f"🚀 최종 시스템 가동!")
print(f"   - RFID 인증 -> 카메라 ON -> 스쿠터(>80%) 확인 -> 촬영")
print("==================================================")

dgstreams.Composition(*pipeline).start()