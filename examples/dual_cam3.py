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
import signal
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

AI_SAME_RATE = 50.0

mqtt_client = None
pir = MotionSensor(PIR_PIN)
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)
stop_event = threading.Event()

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
        self.led_status = "IDLE"
        self.capture_start_time = 0 
        
        # [핵심] 카메라별 재부팅 플래그 분리 (0번용, 1번용)
        self.reset_flags = [False, False]

state = SystemState()

# --- [로그 헬퍼] ---
def log(tag, msg):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{tag}] {msg}")

# --- [기능 함수들] ---
def play_buzzer(count):
    if not buzzer: return
    def _beep():
        for _ in range(count):
            if stop_event.is_set(): break
            buzzer.value = 0.5 
            time.sleep(0.15) 
            buzzer.value = 0   
            time.sleep(0.1)  
    threading.Thread(target=_beep, daemon=True).start()

def play_finish_sound():
    if not buzzer: return
    def _sequence():
        for _ in range(2):
            if stop_event.is_set(): return
            buzzer.value = 0.5
            time.sleep(0.15)
            buzzer.value = 0
            time.sleep(0.1)
        time.sleep(1.0) 
        for _ in range(3):
            if stop_event.is_set(): return
            buzzer.value = 0.5
            time.sleep(0.15)
            buzzer.value = 0
            time.sleep(0.1)
    threading.Thread(target=_sequence, daemon=True).start()

def extend_relay(seconds):
    target_time = time.time() + seconds
    if target_time > state.relay_off_time:
        state.relay_off_time = target_time

# --- [스레드 1: 릴레이 & 와치독] ---
def relay_manager_thread():
    log("THREAD", "Relay Manager Started")
    while not stop_event.is_set():
        # 릴레이 제어
        if time.time() < state.relay_off_time:
            if not relay.value: relay.on()
        else:
            if relay.value: relay.off()
            
        # [와치독: 타임아웃 감지 및 복구 명령]
        if state.mode == "CAPTURING":
            elapsed = time.time() - state.capture_start_time
            # 12초가 지나도 안 끝나면 리셋 (네트워크 지연 고려)
            if elapsed > 12.0:
                log("WATCHDOG", f"🚨 TIMEOUT ({elapsed:.1f}s)! Triggering Camera Reset.")
                
                with state.lock:
                    state.mode = "IDLE"
                    state.rfid_data = None
                    state.request_id = None
                    state.finished_count = 0
                    # 두 카메라 모두에게 재부팅 지시
                    state.reset_flags = [True, True]
                
                if buzzer:
                    buzzer.value = 0.5
                    time.sleep(0.5)
                    buzzer.value = 0
        time.sleep(0.1)

# --- [스레드 2: PIR 센서] ---
def pir_monitor_thread():
    while not stop_event.is_set():
        try:
            if pir.value:
                extend_relay(30.0) 
        except Exception:
            break
        time.sleep(0.2)

# --- [LED 효과] ---
def color_wipe(color, wait):
    for i in range(LED_COUNT):
        if stop_event.is_set(): return
        if pixels:
            pixels[i] = color
            pixels.show()
        time.sleep(wait)

def led_manager_thread():
    if not pixels: return
    log("THREAD", "LED Manager Started")
    
    def set_color(color):
        pixels.fill(color)
        pixels.show()

    current_led_mode = ""

    while not stop_event.is_set():
        if current_led_mode != state.mode:
            current_led_mode = state.mode

        if state.mode == "IDLE":
            color_wipe((0, 255, 105), 0.1)
            time.sleep(0.5)
        elif state.mode == "WAIT_FOR_TAG":
            set_color((0, 0, 255)) 
            time.sleep(0.5)
            set_color((0, 0, 0))   
            time.sleep(0.5)
        elif state.mode == "CAPTURING":
            set_color((255, 0, 0)) 
            time.sleep(0.1)
            
    set_color((0,0,0))

# --- [스레드 3: MQTT] ---
def run_mqtt_thread():
    log("THREAD", "MQTT Thread Started")
    def on_connect(client, userdata, flags, rc):
        log("MQTT", f"Connected (rc={rc})")
        client.subscribe(MQTT_TOPIC_TRIGGER)

    def on_message(client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            log("MQTT", f"Received: {payload_str}")
            
            try:
                data = json.loads(payload_str)
                command = data.get('command')
                req_id = data.get('requestId')
            except json.JSONDecodeError:
                command = payload_str
                req_id = "unknown"

            if command == 'start':
                if state.mode == "IDLE":
                    log("MQTT", f"Start Command Accepted (ID: {req_id})")
                    play_buzzer(1)
                    state.request_id = req_id 
                    state.mode = "WAIT_FOR_TAG"
                elif state.mode == "WAIT_FOR_TAG":
                    log("MQTT", "⚠️ Ignored: Already waiting for tag")
                else:
                    log("MQTT", f"⚠️ Ignored: System busy ({state.mode})")
        except Exception as e:
            log("MQTT", f"Message Error: {e}")

    global mqtt_client
    try:
        mqtt_client = mqtt.Client() 
    except:
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    log("MQTT", "Connecting to broker...")
    try:
        mqtt_client.connect(BROKER_ADDRESS, 1883, 60)
        while not stop_event.is_set():
            mqtt_client.loop(0.1)
    except Exception as e:
        log("MQTT", f"Connection Failed: {e}")

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
            
            if len(data) > 8:
                hex_str = data.hex().upper()
                if hex_str.startswith("BB"):
                    if state.mode == "WAIT_FOR_TAG":
                        if TARGET_RFID_TAG in hex_str:
                            log("RFID", "✅ Tag Detected! Starting Capture.")
                            
                            play_buzzer(1) 
                            if pixels:
                                pixels.fill((0, 255, 0))
                                pixels.show()
                                time.sleep(0.5)

                            with state.lock:
                                state.finished_count = 0
                                state.rfid_data = TARGET_RFID_TAG
                                state.mode = "CAPTURING"
                                state.capture_start_time = time.time() 

    except Exception as e:
        log("RFID", f"Error: {e}")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

# --- [5. 카메라 제너레이터 (핵심: 순차 재부팅 + 부하 분산)] ---
def picamera_generator(index):
    # [부팅 충돌 방지] 0.5초 간격으로 순차 실행
    time.sleep(index * 0.5)
    log("CAM_GEN", f"Initializing Camera {index} (Staggered Start)")
    picam2 = None

    def start_camera():
        p = Picamera2(index)
        config = p.create_preview_configuration(main={"size": (640, 480)})
        p.configure(config)
        p.start()
        return p

    try:
        picam2 = start_camera()
        log("CAM_GEN", f"✅ Camera {index} Ready")

        while not stop_event.is_set():
            
            # [복구 로직] 내 전용 리셋 플래그 확인
            if state.reset_flags[index]:
                log("CAM_GEN", f"⚠️ Camera {index} Resetting...")
                
                # 1. 끄기
                if picam2:
                    try: picam2.stop(); picam2.close()
                    except: pass
                picam2 = None
                
                # 2. 대기 (재부팅 시에도 순차 실행)
                time.sleep(1.0 + (index * 0.5)) 
                
                # 3. 켜기
                try:
                    picam2 = start_camera()
                    log("CAM_GEN", f"✅ Camera {index} Recovered!")
                    # [중요] 내 할 일 끝났으므로 내 플래그만 내림 (무한 루프 방지)
                    state.reset_flags[index] = False 
                except Exception as e:
                    log("CAM_GEN", f"❌ Camera {index} Recovery Failed: {e}")
                    state.reset_flags[index] = False

            # [촬영 로직]
            try:
                if picam2:
                    frame_rgb = picam2.capture_array()
                    
                    if state.mode == "CAPTURING":
                        extend_relay(1.0) 
                        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                        yield frame_bgr
                        # [중요] 0.03초 대기 (전력 부하 분산)
                        time.sleep(0.03) 
                    else:
                        # IDLE 상태일 때
                        time.sleep(0.01)
            except Exception as e:
                log("CAM_GEN", f"⚠️ Camera {index} Frame Error: {e}")
                time.sleep(0.1)
    
    except Exception as e:
        log("CAM_GEN", f"❌ Camera {index} Fatal Error: {e}")
    finally:
        if picam2:
            try: picam2.stop(); picam2.close()
            except: pass

# --- [6. 스마트 촬영 Gizmo (스레드 풀 + 세션)] ---
class SmartCaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.has_shot = False 
        # 동시 전송 제한 (2개) 및 연결 재사용
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.session = requests.Session()
        self.session.verify = False

    def run(self):
        log("GIZMO", f"[{self.camera_name}] Started")
        for result_wrapper in self.get_input(0):
            if stop_event.is_set() or self._abort: break
            
            if state.mode != "CAPTURING":
                if self.has_shot:
                    self.has_shot = False

            if state.mode == "CAPTURING" and not self.has_shot:
                log("GIZMO", f"[{self.camera_name}] 📸 Triggered!")
                
                self.executor.submit(self.save_and_send_thread, 
                                     result_wrapper.data.copy(),
                                     state.rfid_data,
                                     state.request_id)

                self.has_shot = True 
                
                with state.lock:
                    state.finished_count += 1
                    log("GIZMO", f"Progress: {state.finished_count} / {len(configurations)}")
                    
                    if state.finished_count >= len(configurations):
                        log("GIZMO", "✅ All Cameras Finished!")
                        play_finish_sound()

                        if pixels:
                            pixels.fill((0, 255, 0)) 
                            pixels.show()
                            time.sleep(2.0)
                            pixels.fill((0, 0, 0))
                            pixels.show()

                        self.send_complete_mqtt()

                        log("GIZMO", "Returning to IDLE")
                        state.mode = "IDLE"
                        state.rfid_data = None
                        state.request_id = None
            
            self.send_result(result_wrapper)
            
        self.executor.shutdown(wait=False)
        self.session.close()

    def send_complete_mqtt(self):
        try:
            if mqtt_client:
                payload = json.dumps({
                    "command": "complete",
                    "requestId": state.request_id,
                    "status": "success",
                    "message": "Upload complete"
                })
                mqtt_client.publish(MQTT_TOPIC_COMPLETE, payload)
                log("MQTT", "Sent Complete Signal")
        except Exception as e:
            log("MQTT", f"Complete Signal Error: {e}")

    def save_and_send_thread(self, image_array, rfid_data, req_id):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.camera_name}_RETURN_{timestamp}.jpg"
            
            _, img_encoded = cv2.imencode('.jpg', image_array)
            files = {'imageFile': (filename, img_encoded.tobytes(), 'image/jpeg')}
            data = {
                'camera': self.camera_name,
                'rfid': rfid_data,
                'status': 'return_complete',
                'requestId': req_id
            }
            self.session.post(SERVER_LINK, files=files, data=data, timeout=10.0)
            log("UPLOAD", f"[{self.camera_name}] ✅ Success!")
        except Exception as e:
            log("UPLOAD", f"[{self.camera_name}] ❌ Failed: {e}")

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
notifiers = [SmartCaptureGizmo(cfg["display_name"]) for cfg in configurations]
display = dgstreams.VideoDisplayGizmo(
    [cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True
)

pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> notifier >> display[di] for di, (detector, notifier) in enumerate(zip(detectors, notifiers))),
)

if __name__ == "__main__":
    threads = []
    t_mqtt = threading.Thread(target=run_mqtt_thread, daemon=True)
    t_rfid = threading.Thread(target=rfid_reader_thread, daemon=True)
    t_relay = threading.Thread(target=relay_manager_thread, daemon=True)
    t_pir = threading.Thread(target=pir_monitor_thread, daemon=True)
    t_led = threading.Thread(target=led_manager_thread, daemon=True)

    threads.extend([t_mqtt, t_rfid, t_relay, t_pir, t_led])
    for t in threads: t.start()

    log("MAIN", "🚀 System Started (Final Optimized Version)")

    pipeline_obj = dgstreams.Composition(*pipeline)

    try:
        pipeline_obj.start()
    except KeyboardInterrupt:
        log("MAIN", "🛑 Shutdown Requested")
        
        stop_event.set()
        
        log("MAIN", "Stopping Pipeline...")
        pipeline_obj.stop() 
        
        for t in threads:
            t.join(timeout=1.0)
        
        if pixels: pixels.fill((0,0,0)); pixels.show()
        if buzzer: buzzer.value = 0
        relay.off()
        
        if mqtt_client:
            try: mqtt_client.disconnect()
            except: pass

        log("MAIN", "👋 Bye!")
        sys.exit(0)