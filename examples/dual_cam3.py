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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [사용자 설정] ---
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
PIR_PIN = 17
RELAY_PIN = 27
BUZZER_PIN = 22
LED_PIN = board.D18 
LED_COUNT = 14 
LED_BRIGHTNESS = 0.1 

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# --- [전역 상태 관리] ---
class SystemState:
    def __init__(self):
        self.mode = "IDLE" 
        self.rfid_data = None
        self.finished_count = 0 
        self.lock = threading.Lock()
        self.relay_off_time = 0.0
        self.request_id = None
        
        self.capture_start_time = 0 
        self.completed_cameras = set()
        self.total_cameras = 0
        
        # 카메라 리셋 플래그
        self.reset_flags = [False, False]

state = SystemState()
stop_event = threading.Event()

# --- [하드웨어 초기화] ---
pir = MotionSensor(PIR_PIN)
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)
mqtt_client = None

try: buzzer = PWMOutputDevice(BUZZER_PIN, frequency=2000, initial_value=0)
except: buzzer = None

try: pixels = neopixel.NeoPixel(LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS, auto_write=False)
except: pixels = None

# --- [헬퍼 함수] ---
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
        for _ in range(2):
            if stop_event.is_set(): return
            buzzer.value = 0.5; time.sleep(0.15)
            buzzer.value = 0; time.sleep(0.1)
        time.sleep(1.0)
        for _ in range(3):
            if stop_event.is_set(): return
            buzzer.value = 0.5; time.sleep(0.15)
            buzzer.value = 0; time.sleep(0.1)
    threading.Thread(target=_sequence, daemon=True).start()

def extend_relay(seconds):
    target_time = time.time() + seconds
    if target_time > state.relay_off_time:
        state.relay_off_time = target_time

# --- [스레드 1: 릴레이 & 와치독] ---
def relay_manager_thread():
    log("THREAD", "Relay Manager Started")
    while not stop_event.is_set():
        if time.time() < state.relay_off_time:
            if not relay.value: relay.on()
        else:
            if relay.value: relay.off()
            
        # [와치독] 
        # 카메라 켜지는 시간(약 2초) + 딜레이(2.5초) + 전송시간 고려 -> 15초 타임아웃
        if state.mode == "CAPTURING":
            elapsed = time.time() - state.capture_start_time
            if elapsed > 15.0:
                log("WATCHDOG", "🚨 Timeout (15s). Resetting.")
                with state.lock:
                    state.mode = "IDLE"
                    state.rfid_data = None
                    state.completed_cameras.clear()
                    state.reset_flags = [True, True]
                if buzzer: buzzer.value = 0.5; time.sleep(0.5); buzzer.value = 0
        time.sleep(0.1)

# --- [스레드 2: PIR 및 LED (Color Wipe 복구)] ---
def pir_monitor_thread():
    while not stop_event.is_set():
        try:
            if pir.value: extend_relay(30.0) 
        except: break
        time.sleep(0.2)

# [복구된 Color Wipe 함수]
def color_wipe(color, wait):
    if not pixels: return
    for i in range(LED_COUNT):
        if stop_event.is_set() or state.mode != "IDLE": return
        pixels[i] = color
        pixels.show()
        time.sleep(wait)

def led_manager_thread():
    if not pixels: return
    
    def set_color(color):
        pixels.fill(color); pixels.show()

    current_led_mode = ""
    while not stop_event.is_set():
        if current_led_mode != state.mode:
            current_led_mode = state.mode

        if state.mode == "IDLE":
            # [요청하신대로 복구] IDLE일 때 화려한 효과
            color_wipe((0, 255, 105), 0.05)
            time.sleep(0.1)
            
        elif state.mode == "WAIT_FOR_TAG":
            set_color((0, 0, 255)); time.sleep(0.5)
            set_color((0, 0, 0)); time.sleep(0.5)
            
        elif state.mode == "CAPTURING":
            set_color((255, 0, 0)); time.sleep(0.1)
        
        else:
            time.sleep(0.1)

# --- [스레드 3: MQTT] ---
def run_mqtt_thread():
    log("THREAD", "MQTT Started")
    def on_connect(client, userdata, flags, rc):
        client.subscribe(MQTT_TOPIC_TRIGGER)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if payload.get('command') == 'start' and state.mode == "IDLE":
                log("MQTT", "Command: START")
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
        while not stop_event.is_set(): mqtt_client.loop(0.1)
    except: pass

# --- [스레드 4: RFID 리더] ---
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
                if state.mode == "WAIT_FOR_TAG":
                    if TARGET_RFID_TAG in hex_str:
                        log("RFID", "✅ Tag Detected!")
                        play_buzzer(1)
                        if pixels: pixels.fill((0, 255, 0)); pixels.show(); time.sleep(0.5)

                        with state.lock:
                            state.finished_count = 0
                            state.completed_cameras.clear()
                            state.rfid_data = TARGET_RFID_TAG
                            state.mode = "CAPTURING"
                            state.capture_start_time = time.time()
    except: pass
    finally:
        if 'ser' in locals() and ser.is_open: ser.close()

# --- [5. 카메라 제너레이터 (IDLE=OFF, CAPTURING=ON)] ---
def picamera_generator(index):
    time.sleep(index * 0.5)
    log("CAM", f"Camera {index} Thread Ready")
    
    picam2 = None

    def start_camera():
        try:
            p = Picamera2(index)
            config = p.create_preview_configuration(main={"size": (640, 480)})
            p.configure(config)
            p.start()
            # 켜질 때 안정화 시간
            time.sleep(1.0) 
            return p
        except Exception as e:
            log("CAM", f"Init Fail: {e}")
            return None

    def stop_camera(p):
        if p:
            try: p.stop(); p.close()
            except: pass
        return None

    try:
        while not stop_event.is_set():
            
            # 1. CAPTURING 모드일 때만 카메라 켜기
            if state.mode == "CAPTURING":
                if picam2 is None:
                    log("CAM", f"Camera {index} Starting...")
                    picam2 = start_camera()
                    if picam2: log("CAM", f"Camera {index} ON")
                
                # 카메라가 켜져 있으면 촬영 및 Yield
                if picam2:
                    try:
                        frame_rgb = picam2.capture_array()
                        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                        extend_relay(1.0)
                        yield frame_bgr
                        # 30fps 유지
                        time.sleep(0.01) 
                    except:
                        time.sleep(0.1)
                else:
                    time.sleep(0.5) # 켜기 실패 시 대기

            # 2. IDLE (또는 WAIT) 모드일 때는 카메라 끄기
            else:
                if picam2 is not None:
                    log("CAM", f"Camera {index} Stopping (IDLE)...")
                    picam2 = stop_camera(picam2)
                
                # 카메라는 껐지만 루프는 돌아야 함 (잠시 대기)
                time.sleep(0.5)
                
    except Exception as e:
        log("CAM", f"Fatal Error: {e}")
    finally:
        stop_camera(picam2)

# --- [6. 스마트 Gizmo (2.5초 딜레이 + 무한 재시도)] ---
class SmartCaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.session = requests.Session()
        self.session.verify = False

    def run(self):
        log("GIZMO", f"[{self.camera_name}] Ready")
        
        for result in self.get_input(0):
            if stop_event.is_set(): break
            
            # CAPTURING 모드일 때만 동작
            if state.mode == "CAPTURING" and (self.camera_name not in state.completed_cameras):
                
                # [딜레이 로직]
                elapsed = time.time() - state.capture_start_time
                if elapsed < 2.5:
                    pass # 대기
                else:
                    # 전송 시도
                    success = self.send_image_sync(result.data, state.rfid_data, state.request_id)
                    if success:
                        with state.lock:
                            state.completed_cameras.add(self.camera_name)
                            log("GIZMO", f"[{self.camera_name}] Done ({len(state.completed_cameras)}/{state.total_cameras})")
                            if len(state.completed_cameras) >= state.total_cameras:
                                self.finish_sequence()
            
            self.send_result(result)

    def send_image_sync(self, img, rfid, req_id):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.camera_name}_{timestamp}.jpg"
            _, enc = cv2.imencode('.jpg', img)
            files = {'imageFile': (filename, enc.tobytes(), 'image/jpeg')}
            data = {'camera': self.camera_name, 'rfid': rfid, 'status': 'return_complete', 'requestId': req_id}
            res = self.session.post(SERVER_LINK, files=files, data=data, timeout=1.0)
            return (res.status_code in [200, 201])
        except: return False

    def finish_sequence(self):
        threading.Thread(target=self._finish_logic, daemon=True).start()

    def _finish_logic(self):
        log("SYSTEM", "Finish. Wait 1.5s...")
        play_finish_sound()
        try:
            if mqtt_client: mqtt_client.publish(MQTT_TOPIC_COMPLETE, json.dumps({"status":"success"}))
        except: pass
        
        time.sleep(1.5)
        
        with state.lock:
            state.mode = "IDLE"
            state.rfid_data = None
            state.completed_cameras.clear()
        log("SYSTEM", "Reset to IDLE")

# --- [메인 실행] ---
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

    log("MAIN", "System Started (Cam OFF in IDLE)")

    pipeline_obj = dgstreams.Composition(*pipeline)

    try:
        pipeline_obj.start()
    except KeyboardInterrupt:
        stop_event.set()
        pipeline_obj.stop()
        for t in threads: t.join(timeout=1.0)
        if pixels: pixels.fill((0,0,0)); pixels.show()
        if buzzer: buzzer.value = 0
        relay.off()
        if mqtt_client:
            try: mqtt_client.disconnect()
            except: pass
        sys.exit(0)