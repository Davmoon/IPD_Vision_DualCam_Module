import degirum as dg
import degirum_tools
import degirum_tools.streams as dgstreams
from picamera2 import Picamera2
import cv2  # 색상 변환을 위해 OpenCV가 필요합니다.

# -----------------------------------------------------------------
# 1. Picamera2 프레임 제너레이터 (Snippet 1 수정)
#    Pi 카메라에서 프레임을 캡처하여 DeGirum이 원하는 BGR 형식으로 변환
# -----------------------------------------------------------------
def picamera_generator():
    picam2 = Picamera2(1)
    # 모델 입력 크기(640x640)와 유사하게 설정 (필요시 조절)
    config = picam2.create_preview_configuration(main={"size": (640, 640)}) 
    picam2.configure(config)
    picam2.start()
    print("✅ PiCamera2 제너레이터 시작됨...")
    try:
        while True:
            # (1) 프레임 캡처 (RGB 형식)
            frame_rgb = picam2.capture_array()
            
            # (2) DeGirum 모델이 요구하는 BGR 형식으로 변환
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            
            # (3) BGR 프레임 전달
            yield frame_bgr
    finally:
        picam2.stop()
        print("🛑 PiCamera2 제너레이터 중지됨.")

# -----------------------------------------------------------------
# 2. DeGirum 설정 (Snippet 2)
# -----------------------------------------------------------------
inference_host_address = "@local"
zoo_url = "../models"
token = ''
model_name = "yolov8n_relu6_coco--640x640_quant_hailort_hailo8l_1"

# -----------------------------------------------------------------
# 3. 모델 로드
#    두 개의 스트림(파일, 카메라)을 위해 모델 인스턴스 2개 로드
# -----------------------------------------------------------------
print("Hailo 모델 로드 중...")
model_file = dg.load_model(model_name, inference_host_address, zoo_url, token)
model_cam = dg.load_model(model_name, inference_host_address, zoo_url, token)
print("✅ 모델 로드 완료.")

# -----------------------------------------------------------------
# 4. Gizmo 정의 (가장 중요한 부분)
# -----------------------------------------------------------------
print("Gizmo 파이프라인 정의 중...")
# 소스 1: 비디오 파일 (기존 VideoSourceGizmo 사용)
source_file = dgstreams.VideoSourceGizmo("../assets/Traffic.mp4")

# 소스 2: Pi 카메라 (GeneratorSourceGizmo 사용)
# ‼️ VideoSourceGizmo 대신 GeneratorSourceGizmo를 사용합니다.
source_cam = dgstreams.IteratorSourceGizmo(picamera_generator())
# 탐지기 2개
detector_file = dgstreams.AiSimpleGizmo(model_file)
detector_cam = dgstreams.AiSimpleGizmo(model_cam)

# 디스플레이 1개 (2개 입력을 받음)
display = dgstreams.VideoDisplayGizmo(
    ["Traffic Camera", "Webcam Feed"],  # 창 제목
    show_ai_overlay=True, 
    show_fps=True
)

# -----------------------------------------------------------------
# 5. 파이프라인 생성 (스트림 2개 연결)
# -----------------------------------------------------------------
pipeline = (
    # 첫 번째 스트림: 파일 -> 탐지기 -> 디스플레이 0번
    source_file >> detector_file,
    detector_file >> display[0],
    
    # 두 번째 스트림: Pi카메라 -> 탐지기 -> 디스플레이 1번
    source_cam >> detector_cam,
    detector_cam >> display[1],
)

# -----------------------------------------------------------------
# 6. 파이프라인 시작
# -----------------------------------------------------------------
print("✅ 파이프라인 시작! (Ctrl+C로 종료)")
dgstreams.Composition(*pipeline).start()