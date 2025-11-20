import degirum as dg
import degirum_tools
import degirum_tools.streams as dgstreams
from picamera2 import Picamera2
import cv2
import time
import requests
import sys
import os
from datetime import datetime
from gpiozero import MotionSensor

# inference_host_address = "@cloud"
inference_host_address = "@local"

# choose zoo_url
#zoo_url = "degirum/models_hailort"
zoo_url = "../models"

# set token
#token = degirum_tools.get_token()
token = '' # leave empty for local inference

# 이미지 전송 서버 주소
SERVER_LINK = "https://davmo.xyz/upload"

# 이미지 저장 폴더
SAVE_DIR = "captures"

#PIR 센서 핀
PIR_PIN = 17
pir = MotionSensor(PIR_PIN)

# 저장 폴더 없으면 생성하도록
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

def picamera_generator(index):
    picam2 = Picamera2(index)
    config = picam2.create_preview_configuration(main={"size": (640, 480)}) 
    picam2.configure(config)
    picam2.start()
    time.sleep(1.0)
    try:
        while True:
            frame_rgb = picam2.capture_array()
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            yield frame_bgr
    finally:
        picam2.stop()

class NotificationGizmo(dgstreams.Gizmo):
    def __init__(self, camera_name):
        super().__init__([(10,)])
        self.camera_name = camera_name
        self.frame_count = 0
        self.last_save_time = 0

    def run(self):
        #print(f"[{self.camera_name}]")
        
        for result_wrapper in self.get_input(0):
            if self._abort:
                break
            
            inf_result = None

            #예외처리를 위해 속성 먼저 검색.
            if hasattr(result_wrapper.data, 'result'):
                inf_result = result_wrapper.data
            else:
                try:
                    for item in result_wrapper.meta._meta_list:
                        if hasattr(item, 'results'):
                            inf_result = item
                            break
                except: pass

            if inf_result and inf_result.results:
                for obj in inf_result.results:
                    label = obj.get('label', '')
                    score = obj.get('score', 0) * 100

                    if 'scooter' in label and score >= 80.0:
                        print(f"\n[{self.camera_name}] found. type:'{label}' ({score:.1f}%)", flush=True)

                        if time.time() - self.last_save_time > 2.0:
                            self.save_and_send(result_wrapper.data, label, score)
                            self.last_save_time = time.time()

            #시간 지날때마다 프레임 카운트해서 점 찍음(진행상황 파악.)
            self.frame_count += 1
            if self.frame_count % 180 == 0:
                print(".", end="", flush=True)
            
            self.send_result(result_wrapper)

    #이미지를 저장하고 서버로 전송하는 함수
    def save_and_send(self, image_array, label, score):
            try:
                # 1. 파일명 생성 (예: captures/cam0_scooter_20231025_123001.jpg)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{self.camera_name}_{label.replace(' ', '_')}_{timestamp}.jpg"
                filepath = os.path.join(SAVE_DIR, filename)

                # 2. 로컬 저장 (OpenCV 사용)
                cv2.imwrite(filepath, image_array)
                print(f"   💾 저장 완료: {filepath}")

                # 3. 서버 전송 (Requests 사용)
                # 이미지를 메모리상에서 jpg로 인코딩 (파일 다시 읽는 것보다 빠름)
                _, img_encoded = cv2.imencode('.jpg', image_array)
                files = {
                    'file': (filename, img_encoded.tobytes(), 'image/jpeg')
                }
                data = {
                    'camera': self.camera_name,
                    'label': label,
                    'score': f"{score:.1f}"
                }
                
                # 타임아웃 1초 설정 (서버가 응답 없어도 1초 뒤에 무시하고 계속 진행)
                response = requests.post(SERVER_LINK, files=files, data=data, timeout=1.0)
                
                if response.status_code == 200:
                    print(f"   📡 서버 전송 성공! (200 OK)")
                else:
                    print(f"   ⚠️ 서버 전송 실패 (Code: {response.status_code})")

            except Exception as e:
                # 에러가 나도 프로그램이 멈추지 않도록 예외 처리
                print(f"   ❌ 저장/전송 중 오류 발생: {e}")

# Define the configurations for video file and webcam
configurations = [
    {
        "model_name": "scooter_model",
        "source" : '0',
        "display_name": "cam0",
    },
    {
        "model_name": "scooter_model",
        "source" : '1',
        "display_name": "cam1",
    },
]


# load models
models = [
    dg.load_model(cfg["model_name"], inference_host_address, zoo_url, token)
    for cfg in configurations
]

# define gizmos
sources = [dgstreams.IteratorSourceGizmo(picamera_generator(int(cfg["source"]))) for cfg in configurations]
detectors = [dgstreams.AiSimpleGizmo(model) for model in models]
notifiers = [NotificationGizmo(cfg["display_name"]) for cfg in configurations]
display = dgstreams.VideoDisplayGizmo(
    [cfg["display_name"] for cfg in configurations], show_ai_overlay=True, show_fps=True
)

# create pipeline
pipeline = (
    (source >> detector for source, detector in zip(sources, detectors)),
    (detector >> notifiers >> display[di] for di, (detector, notifiers) in enumerate(zip(detectors, notifiers))),
)

# start composition
dgstreams.Composition(*pipeline).start()