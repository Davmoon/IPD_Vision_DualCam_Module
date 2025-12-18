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
from gpiozero import MotionSensor, OutputDevice, PWMOutputDevice
import urllib3
import paho.mqtt.client as mqtt
import json
import board
import neopixel
from concurrent.futures import ThreadPoolExecutor

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# [사용자 설정]
# ==========================================
inference_host_address = "@local"
zoo_url = "../models"
token = '' 

SERVER_LINK = "https://davmo.xyz/api/uploads" 
SAVE_DIR = "captures"
TARGET_RFID_TAG = "E2000017570D0173277006CB" 

BROKER_ADDRESS = "broker.emqx.io" 
MQTT_TOPIC_TRIGGER = "davmo/gmatch/camera/trigger"
MQTT_TOPIC_COMPLETE = "davmo/gmatch/camera/complete"

SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

# GPIO 핀 설정
PIR_PIN = 17
RELAY_PIN = 27
BUZZER_PIN = 22
LED_PIN = board.D18 
LED_COUNT = 14 
LED_BRIGHTNESS = 0.1 

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# ==========================================
# [전역 시스템 상태 관리]
# ==========================================
class SystemState:
    def __init__(self):
        self.mode = "IDLE"  # IDLE, WAIT_FOR_TAG, CAPTURING
        self.rfid_data = None
        self.request_id = None
        
        # 동기화 및 타임아웃 관리
        self.lock = threading.Lock()
        self.relay_off_time = 0.0
        
        # 7초 재시도 로직 변수
        self.capture_start_time = 0
        self.completed_cameras = set()  # 전송 성공한 카메라 이름 저장
        self.total_cameras = 0          # 전체 카메라 개수

state = SystemState()
stop_event = threading.Event()

# ==========================================
# [하드웨어 객체 초기화]
# ==========================================
pir = MotionSensor(PIR_PIN)
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)
mqtt_client = None

try:
    buzzer = PWMOutputDevice(BUZZER_PIN, frequency=2000, initial_value=0)
except Exception as e:
    print(f"⚠️ Buzzer Init Failed: {e}")
    buzzer = None

try:
    pixels = neopixel.NeoPixel(LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS, auto_write=False)
except Exception as e:
    print(f"⚠️ NeoPixel Init Failed: {e}")
    pixels = None

# ==========================================
# [헬퍼 함수들: 로그, 소리, 릴레이]
# ==========================================
def log(tag, msg):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{tag}] {msg}")

def play_buzzer(count):
    if not buzzer: return
    def _beep():
        for _ in range(count):
            if stop_event.is_set(): break
            buzzer.value = 0.5; time.sleep(0.15)
            buzzer.value = 0; time.sleep(0.1)
    threading.Thread(target=_beep, daemon=True).start()

def play_finish_sound():
    if not buzzer: return
    def _sequence():
        buzzer.value = 0.5; time.sleep(0.1); buzzer.value = 0; time.sleep(0.05)
        buzzer.value = 0.5; time.sleep(0.1); buzzer.value = 0; time.sleep(0.05)
        buzzer.value = 0.8; time.sleep(0.3); buzzer.value = 0
    threading.Thread(target=_sequence, daemon=True).start()

def extend_relay(seconds):
    target_time = time.time() + seconds
    if target_time > state.relay_off_time:
        state.relay_off_time = target_time

# ==========================================
# [스레드 1: 릴레이 관리 & 7초 타임아웃 감시]
# ==========================================
def relay_manager_thread():
    log("THREAD", "Relay & Watchdog Started")
    while not stop_event.is_set():
        # 1. 릴레이 제어
        if time.time() < state.relay_off_time:
            if not relay.value: relay.on()
        else:
            if relay.value: relay.off()
            
        # 2. [중요] 7초 타임아웃 감시
        # 캡처 중인데 7초가 지났다? -> 강제 초기화
        if state.mode == "CAPTURING":
            elapsed = time.time() - state.capture_start_time
            if elapsed > 7.0: 
                log("WATCHDOG", f"🚨 TIMEOUT (7s)! Resetting System.")
                
                with state.lock:
                    state.mode = "IDLE"
                    state.rfid_data = None
                    state.completed_cameras.clear()
                
                # 실패 알림음 (낮은 톤)
                if buzzer:
                    buzzer.frequency = 500
                    buzzer.value = 0.5
                    time.sleep(0.5)
                    buzzer.value = 0
                    buzzer.frequency = 2000
        
        time.sleep(0.1)

# ==========================================
# [스레드 2: LED 및 기타]
# ==========================================
def pir_monitor_thread():
    while not stop_event.is_set():
        try:
            if pir.value: extend_relay(30.0)
        except: break
        time.sleep(0.2)

def led_manager_thread():
    if not pixels: return
    
    def set_color(color):
        pixels.fill(color); pixels.show()

    while not stop_event.is_set():
        if state.mode == "IDLE":
            pixels.fill((0, 50, 0)); pixels.show() # 녹색 대기
            time.sleep(0.5)
        elif state.mode == "WAIT_FOR_TAG":
            set_color((0, 0, 255)); time.sleep(0.2) # 파란 깜빡임
            set_color((0, 0, 0)); time.sleep(0.2)
        elif state.mode == "CAPTURING":
            set_color((255, 0, 0)); time.sleep(0.1) # 빨강 (전송중)
        else:
            time.sleep(0.1)

# ==========================================
# [스레드 3: MQTT]
# ==========================================
def run_mqtt_thread():
    log("THREAD", "MQTT Started")
    
    def on_connect(client, userdata, flags, rc):
        client.subscribe(MQTT_TOPIC_TRIGGER)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if payload.get('command') == 'start' and state.mode == "IDLE":
                log("MQTT", "Start Command Received")
                play_buzzer(1)
                with state.lock:
                    state.request_id = payload.get('requestId', 'unknown')
                    state.mode = "WAIT_FOR_TAG"
        except: pass

    global mqtt_client
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(BROKER_ADDRESS, 1883, 60)
        mqtt_client.loop_forever()
    except: pass

# ==========================================
# [스레드 4: RFID 리더]
# ==========================================
def rfid_reader_thread():
    log("THREAD", "RFID Reader Started")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.05)
        cmd_read = bytes.fromhex('BB 00 22 00 00 22 7E')
        
        while not stop_event.is_set():
            ser.reset_input_buffer()
            ser.write(cmd_read)
            data = ser.read(32)
            
            if len(data) > 8 and data.hex().upper().startswith("BB"):
                hex_str = data.hex().upper()
                # 대기 상태일 때 태그 인식
                if state.mode == "WAIT_FOR_TAG":
                    if TARGET_RFID_TAG in hex_str:
                        log("RFID", "✅ Valid Tag Detected!")
                        play_buzzer(1)
                        
                        # [상태 변경] 즉시 캡처 모드로 진입
                        with state.lock:
                            state.completed_cameras.clear()
                            state.rfid_data = TARGET_RFID_TAG
                            state.capture_start_time = time.time()
                            state.mode = "CAPTURING" # 이때부터 Gizmo가 전송 시작
            time.sleep(0.05)
    except Exception as e:
        log("RFID", f"Error: {e}")

# ==========================================
# [핵심 1] 카메라 제너레이터 (무조건 계속 찍음)
# ==========================================
def picamera_generator(index):
    time.sleep(index * 1.0) # 카메라 충돌 방지 딜레이
    log("CAM", f"Camera {index} Init...")
    picam2 = None
    
    try:
        picam2 = Picamera2(index)
        config = picam2.create_preview_configuration(main={"size": (640, 480)})
        picam2.configure(config)
        picam2.start()
        log("CAM", f"✅ Camera {index} Streaming (Always On)")

        while not stop_event.is_set():
            try:
                # [수정됨] 조건문 없음. 무조건 찍어서 보냄.
                # 그래야 화면이 항상 나옴.
                frame_rgb = picam2.capture_array()
                
                # 캡처 중일 때 조명 켜주기
                if state.mode == "CAPTURING":
                    extend_relay(1.0)
                
                # DeGirum 파이프라인으로 프레임 전달
                yield cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                
            except Exception as e:
                log("CAM", f"Err {index}: {e}")
                time.sleep(0.1)
                
    except Exception as e:
        log("CAM", f"Fail {index}: {e}")
    finally:
        if picam2:
            try: picam2.stop(); picam2.close()
            except: pass

# ==========================================
# [핵심 2] 스마트 전송 로직 (화면은 계속, 전송은 조건부)
# ==========================================
class SmartCaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.session = requests.Session()
        self.session.verify = False

    def run(self):
        log("GIZMO", f"[{self.camera_name}] Ready")
        
        # 파이프라인에서 프레임이 쉴 새 없이 들어옴
        for result in self.get_input(0):
            if stop_event.is_set(): break
            
            # 1. 캡처 모드이고, 아직 내 카메라가 성공 안 했으면 전송 시도
            if state.mode == "CAPTURING" and (self.camera_name not in state.completed_cameras):
                
                # 전송 시도 (성공 여부 반환)
                success = self.send_image_sync(result.data, state.rfid_data, state.request_id)
                
                if success:
                    with state.lock:
                        state.completed_cameras.add(self.camera_name)
                        log("GIZMO", f"[{self.camera_name}] ✅ Upload Done! ({len(state.completed_cameras)}/{state.total_cameras})")
                        
                        # 모든 카메라 성공 시
                        if len(state.completed_cameras) >= state.total_cameras:
                            self.finish_sequence()
                else:
                    # 실패 시 로그만 찍음 -> 다음 루프(다음 프레임)에서 자동으로 다시 시도됨
                    # log("GIZMO", f"[{self.camera_name}] Retry...")
                    pass

            # 2. [중요] 전송 여부와 상관없이 무조건 화면으로 넘김
            # 이게 있어야 창이 안 멈춤
            self.send_result(result)

    def send_image_sync(self, img, rfid, req_id):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.camera_name}_{timestamp}.jpg"
            _, enc = cv2.imencode('.jpg', img)
            
            files = {'imageFile': (filename, enc.tobytes(), 'image/jpeg')}
            data = {'camera': self.camera_name, 'rfid': rfid, 'status': 'return_complete', 'requestId': req_id}
            
            # 타임아웃 1초 (빨리 실패하고 다음 프레임으로 재시도하는 게 나음)
            res = self.session.post(SERVER_LINK, files=files, data=data, timeout=1.0)
            return (res.status_code in [200, 201])
        except:
            return False

    def finish_sequence(self):
        log("SYSTEM", "🎉 All Uploads Complete!")
        play_finish_sound()
        state.mode = "IDLE"
        state.rfid_data = None
        state.completed_cameras.clear()
        
        try:
            if mqtt_client:
                mqtt_client.publish(MQTT_TOPIC_COMPLETE, json.dumps({"status":"success"}))
        except: pass

# ==========================================
# [메인 실행]
# ==========================================
# 카메라 2대 설정 (0번, 1번)
configurations = [
    { "model_name": "scooter_model", "source" : 0, "display_name": "Camera 0" },
    { "model_name": "scooter_model", "source" : 1, "display_name": "Camera 1" },
]
state.total_cameras = len(configurations)

models = [dg.load_model(cfg["model_name"], inference_host_address, zoo_url, token) for cfg in configurations]

sources = [dgstreams.IteratorSourceGizmo(picamera_generator(int(cfg["source"]))) for cfg in configurations]
detectors = [dgstreams.AiSimpleGizmo(model) for model in models]
notifiers = [SmartCaptureGizmo(cfg["display_name"]) for cfg in configurations]
display = dgstreams.VideoDisplayGizmo([cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True)

pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> notifier >> display[di] for di, (detector, notifier) in enumerate(zip(detectors, notifiers))),
)

if __name__ == "__main__":
    threads = [
        threading.Thread(target=rfid_reader_thread, daemon=True),
        threading.Thread(target=relay_manager_thread, daemon=True),
        threading.Thread(target=pir_monitor_thread, daemon=True),
        threading.Thread(target=led_manager_thread, daemon=True),
        threading.Thread(target=run_mqtt_thread, daemon=True)
    ]
    for t in threads: t.start()

    log("MAIN", "🚀 Pipeline Starting... Windows should appear now.")
    
    pipeline_obj = dgstreams.Composition(*pipeline)
    
    try:
        pipeline_obj.start() # 여기서 창이 뜨고 계속 유지됨
    except KeyboardInterrupt:
        stop_event.set()
        pipeline_obj.stop()
        sys.exit(0)