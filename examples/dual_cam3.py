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
from gpiozero import MotionSensor, OutputDevice, Buzzer
import urllib3
import paho.mqtt.client as mqtt
import json
import board
import neopixel

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- [사용자 설정] ---
inference_host_address = "@local"
zoo_url = "../models"
token = '' 

SERVER_LINK = "https://davmo.xyz/api/uploads" 
SAVE_DIR = "captures"

# RFID 태그 ID
TARGET_RFID_TAG = "E2000017570D0173277006CB" 

# MQTT 설정
BROKER_ADDRESS = "broker.emqx.io"  
MQTT_TOPIC = "davmo/gmatch/camera/trigger"

# 하드웨어 핀 설정
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200
PIR_PIN = 17
RELAY_PIN = 27
BUZZER_PIN = 22

# [추가] NeoPixel 설정
LED_PIN = board.D18  # GPIO 18
LED_COUNT = 14       # LED 바의 개수 (사용하는 제품에 맞게 수정하세요! 보통 8개)
LED_BRIGHTNESS = 0.1 # 밝기 (0.0 ~ 1.0)

AI_SAME_RATE = 50.0

pir = MotionSensor(PIR_PIN)
relay = OutputDevice(RELAY_PIN, active_high=True, initial_value=False)
buzzer = Buzzer(BUZZER_PIN)

try:
    pixels = neopixel.NeoPixel(LED_PIN, LED_COUNT, brightness=LED_BRIGHTNESS, auto_write=False)
except Exception as e:
    print(f"NeoPixel 초기화 실패 : {e}")
    pixels = None

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# --- [전역 상태 관리] ---
class SystemState:
    def __init__(self):
        # 상태: IDLE -> WAIT_FOR_TAG -> CAPTURING
        self.mode = "IDLE" 
        self.rfid_data = None
        self.finished_count = 0 
        self.lock = threading.Lock()
        self.relay_off_time = 0.0
        self.request_id = None
        self.led_status = "IDLE" # IDLE, WAITING, BUSY, SUCCESS

state = SystemState()

def play_buzzer(count):
    def _beep():
        for _ in range(count):
            buzzer.on()
            time.sleep(0.1) # 삐
            buzzer.off()
            time.sleep(0.1) # 멈춤
    
    # 메인 로직이 멈추지 않도록 별도 스레드에서 소리 재생
    threading.Thread(target=_beep, daemon=True).start()

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

# [스레드 2] PIR 센서
def pir_monitor_thread():
    while True:
        if pir.value:
            extend_relay(30.0) 
        time.sleep(0.2)

def color_wipe(color, wait):
    """LED가 하나씩 순서대로 켜지는 효과"""
    for i in range(LED_COUNT):
        pixels[i] = color
        pixels.show()
        time.sleep(wait)

# LED 상태 표시 관리자
def led_manager_thread():
    if not pixels:
        return

    print("NeoPixel LED 제어 시작 (GPIO 18)")
    
    def set_color(color):
        pixels.fill(color)
        pixels.show()

    while True:
        # 시스템 상태(state.mode)에 따라 LED 색상 변경
        
        # idle 상태
        if state.mode == "IDLE":
            set_color((0, 255, 105))
            time.sleep(0.5)

        # 대기중
        elif state.mode == "WAIT_FOR_TAG":
            color_wipe((0, 0, 255), 0.1) # Blue
            time.sleep(0.5)
            set_color((0, 0, 0))   # Off
            time.sleep(0.5)

        #촬영 처리중
        elif state.mode == "CAPTURING":
            color_wipe((255, 0, 0), 0.1) # Red
            time.sleep(0.1)
        
        # 완료 신호(SUCCESS)는 Gizmo에서 잠시 딜레이를 주지 않으면 순식간에 지나가서 안 보임
        # 여기서는 state.mode 위주로 처리

# --- [스레드 3] MQTT 클라이언트 ---
def run_mqtt_thread():
    def on_connect(client, userdata, flags, rc):
        print(f"-- MQTT 브로커 연결됨. (Topic: {MQTT_TOPIC}) --")
        client.subscribe(MQTT_TOPIC)

    def on_message(client, userdata, msg):
        try:
            payload_str = msg.payload.decode()
            print(f"DEBUG: Topic={msg.topic}, Payload={payload_str}")
            
            try:
                data = json.loads(payload_str)
                command = data.get('command')
                req_id = data.get('requestId')
            except json.JSONDecodeError:
                command = payload_str
                req_id = "unknown"

            if command == 'start':
                if state.mode == "IDLE":
                    print(f"\n-- [MQTT] 반납 요청 수신! (ID: {req_id})-- ")
                    state.request_id = req_id 
                    state.mode = "WAIT_FOR_TAG"
                elif state.mode == "WAIT_FOR_TAG":
                    print("-- 이미 태그를 기다리고 있습니다. --")
                else:
                    print(f"-- 시스템이 이미 작동 중입니다. (상태: {state.mode}) --")
                    
        except Exception as e:
            print(f"-- 메시지 처리 중 오류 발생: {e} --")

    try:
        client = mqtt.Client() 
    except:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    client.on_connect = on_connect
    client.on_message = on_message

    print("-- MQTT 접속 시도 중... --")
    try:
        client.connect(BROKER_ADDRESS, 1883, 60)
        client.loop_forever()
    except Exception as e:
        print(f"-- MQTT 연결 오류: {e} --")

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
                    if state.mode == "WAIT_FOR_TAG":
                        if TARGET_RFID_TAG in hex_str:
                            print(f"\n[RFID] 인증 성공. 카메라 기동")

                            play_buzzer(1)
                            
                            if pixels:
                                pixels.fill((0, 255, 0))
                                pixels.show()
                                time.sleep(0.5)

                            with state.lock:
                                state.finished_count = 0
                                state.rfid_data = TARGET_RFID_TAG
                                state.mode = "CAPTURING"
            
            time.sleep(0.05)

    except Exception as e:
        print(f"-- RFID 오류: {e} --")

# --- [5. 카메라 제너레이터] ---
def picamera_generator(index):
    print(f'-- {index}번 카메라 초기화 및 대기 중 (Hot Standby) --')
    
    # [수정] 카메라 객체 생성 및 시작을 루프 밖에서 한 번만 수행
    picam2 = None
    try:
        picam2 = Picamera2(index)
        # 해상도 설정 (AI 모델에 맞춰 최적화)
        config = picam2.create_preview_configuration(main={"size": (640, 480)})
        picam2.configure(config)
        picam2.start() # 카메라는 계속 켜둡니다.
        
        print(f"📷 [{index}번] 하드웨어 준비 완료.")

        while True:
            # [조건] 촬영 모드일 때만 프레임을 AI로 전송
            if state.mode == "CAPTURING":
                
                # 조명 켜기 (지속적으로 시간 연장)
                extend_relay(1.0) 
                
                # 프레임 캡처 및 전송
                frame_rgb = picam2.capture_array()
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                yield frame_bgr
                
            else:
                # [IDLE 상태]
                # 카메라는 켜져있지만, AI로 데이터를 보내지 않고 쉽니다.
                # CPU 사용량을 낮추기 위해 대기
                time.sleep(0.1)

    except Exception as e:
        print(f"❌ [{index}번] 카메라 치명적 오류: {e}")
        # 에러 발생 시 안전하게 닫기
        if picam2:
            try:
                picam2.stop()
                picam2.close()
            except: pass
            
    finally:
        if picam2:
            picam2.stop()
            picam2.close()

# --- [6. 스마트 촬영 Gizmo] ---
class SmartCaptureGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.has_shot = False 

    def run(self):
        for result_wrapper in self.get_input(0):
            if self._abort: break
            
            if state.mode != "CAPTURING":
                self.has_shot = False

            if state.mode == "CAPTURING" and not self.has_shot:
                
                # inf_result = None
                # if hasattr(result_wrapper.data, 'result'):
                #     inf_result = result_wrapper.data
                # else:
                #     try:
                #         for item in result_wrapper.meta._meta_list:
                #             if hasattr(item, 'results'):
                #                 inf_result = item; break
                #     except: pass

                # if inf_result and inf_result.results:
                #     for obj in inf_result.results:
                #         label = obj.get('label', '')
                #         score = obj.get('score', 0) * 100

                #         if 'scooter' in label and score >= 80.0: # (AI_SAME_RATE 대신 80.0 사용)
                #             print(f"\n[{self.camera_name}] 스쿠터 확인됨({score:.1f}%) 사진 촬영")
                            
                            t = threading.Thread(target=self.save_and_send_thread, 
                                                 args=(result_wrapper.data.copy(),
                                                       state.rfid_data,
                                                       state.request_id))
                            t.start()

                            self.has_shot = True 
                            
                            with state.lock:
                                state.finished_count += 1
                                print(f"진행률: {state.finished_count} / {len(configurations)}")
                                
                                if state.finished_count >= len(configurations):
                                    
                                    play_buzzer(2)

                                    # [LED 효과] 완료 시 초록색 2초 유지 후 꺼짐
                                    if pixels:
                                        pixels.fill((0, 255, 0)) # Green
                                        pixels.show()
                                        time.sleep(2.0)
                                        pixels.fill((0, 0, 0))
                                        pixels.show()

                                    print("모든 작업 완료. 대기 모드 전환")
                                    state.mode = "IDLE"
                                    state.rfid_data = None
                                    state.request_id = None
                            
                            #break 
            
            self.send_result(result_wrapper)

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
            
            requests.post(SERVER_LINK, files=files, data=data, timeout=15.0, verify=False)
            print(f"[{self.camera_name}] 전송 완료!")

        except Exception as e:
            print(f"[{self.camera_name}] 전송 오류: {e}")

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

# 스레드 시작
threading.Thread(target=run_mqtt_thread, daemon=True).start()
threading.Thread(target=rfid_reader_thread, daemon=True).start()
threading.Thread(target=relay_manager_thread, daemon=True).start()
threading.Thread(target=pir_monitor_thread, daemon=True).start()
threading.Thread(target=led_manager_thread, daemon=True).start()

print("==================================================")
print(f"🚀 시스템 가동! (LED 바: GPIO 18)")
print("==================================================")

dgstreams.Composition(*pipeline).start()