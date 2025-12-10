import board
import neopixel
import time

# --- [설정] ---
# 핀 번호: GPIO 18 (물리 핀 12번)
PIXEL_PIN = board.D18

# LED 개수: 사용하시는 LED 바의 개수에 맞춰 수정하세요! (보통 8개)
NUM_PIXELS = 14

# 밝기: 0.0 ~ 1.0 (너무 밝으면 눈 아프니 0.2 추천)
BRIGHTNESS = 0.2

# 순서: 대부분 GRB지만, 색이 이상하면 RGB로 바꿔보세요
ORDER = neopixel.GRB 

# 객체 생성
try:
    pixels = neopixel.NeoPixel(
        PIXEL_PIN, 
        NUM_PIXELS, 
        brightness=BRIGHTNESS, 
        auto_write=False, 
        pixel_order=ORDER
    )
except Exception as e:
    print(f"❌ 초기화 오류: {e}")
    print("👉 'sudo python3 test_led.py'로 실행했는지 확인해보세요!")
    exit()

def color_wipe(color, wait):
    """LED가 하나씩 순서대로 켜지는 효과"""
    for i in range(NUM_PIXELS):
        pixels[i] = color
        pixels.show()
        time.sleep(wait)

def main():
    print(f"--- 💡 NeoPixel 테스트 시작 (GPIO 18, {NUM_PIXELS}개) ---")
    print("Ctrl+C를 누르면 종료됩니다.\n")

    try:
        while True:
            print("🔴 빨간색 (RED)")
            pixels.fill((255, 0, 0))
            pixels.show()
            time.sleep(1.0)

            print("🟢 초록색 (GREEN)")
            pixels.fill((0, 255, 0))
            pixels.show()
            time.sleep(1.0)

            print("🔵 파란색 (BLUE)")
            pixels.fill((0, 0, 255))
            pixels.show()
            time.sleep(1.0)

            print("⚪ 흰색 (WHITE)")
            pixels.fill((255, 255, 255))
            pixels.show()
            time.sleep(1.0)

            print("🏃 하나씩 켜기 (Running Light)")
            pixels.fill((0, 0, 0)) # 끄고 시작
            pixels.show()
            color_wipe((255, 0, 0), 0.1) # 빨강으로 채우기
            color_wipe((0, 0, 255), 0.1) # 파랑으로 덮어쓰기
            
            print("🌑 끄기\n")
            pixels.fill((0, 0, 0))
            pixels.show()
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n테스트 종료! LED를 끕니다.")
        pixels.fill((0, 0, 0))
        pixels.show()

if __name__ == "__main__":
    main()