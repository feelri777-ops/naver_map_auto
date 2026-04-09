"""
영역 확인 + 격자 수 자동 계산

사용법:
  1. python find_area.py 실행
  2. 브라우저에서 수동으로: 실내지도 → B3 → 최대 확대
  3. 캡처 영역의 좌상단이 화면 중앙에 오도록 이동 → Enter
  4. 캡처 영역의 우하단이 화면 중앙에 오도록 이동 → Enter
  5. 드래그 거리로 격자 수가 자동 계산됨
"""

import asyncio
import math
from playwright.async_api import async_playwright
import config


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ko-KR",
        )
        page = await context.new_page()

        print("[1] 네이버 지도를 엽니다...")
        await page.goto("https://map.naver.com/", wait_until="networkidle")
        await asyncio.sleep(2)

        print()
        print("=" * 60)
        print(" 수동 작업:")
        print("  1. 신촌세브란스병원 검색")
        print("  2. 실내지도 버튼 클릭 → B3 선택")
        print("  3. 최대로 확대")
        print("=" * 60)
        input("\n완료 후 Enter...")

        # 드래그 거리 추적용 JS 주입
        await page.evaluate("""
            window.__totalDragX = 0;
            window.__totalDragY = 0;
            window.__tracking = false;
            window.__lastX = 0;
            window.__lastY = 0;

            document.addEventListener('mousedown', (e) => {
                if (window.__tracking) {
                    window.__lastX = e.clientX;
                    window.__lastY = e.clientY;
                }
            });
            document.addEventListener('mouseup', (e) => {
                if (window.__tracking) {
                    window.__totalDragX += (window.__lastX - e.clientX);
                    window.__totalDragY += (window.__lastY - e.clientY);
                }
            });
        """)

        # 좌상단 위치 잡기
        print("\n[좌상단] 캡처할 영역의 좌상단이 화면 중앙에 오도록 이동하세요.")
        input("위치를 잡았으면 Enter...")

        # 트래킹 시작
        await page.evaluate("window.__tracking = true;")
        url = page.url
        print(f"  URL: {url}")

        # 우하단 위치 잡기
        print("\n[우하단] 캡처할 영역의 우하단이 화면 중앙에 오도록 이동하세요.")
        print("  (마우스 드래그로 이동 — 드래그 거리가 자동 측정됩니다)")
        input("위치를 잡았으면 Enter...")

        # 트래킹 종료 + 결과 가져오기
        await page.evaluate("window.__tracking = false;")
        total_dx = await page.evaluate("window.__totalDragX")
        total_dy = await page.evaluate("window.__totalDragY")

        print(f"\n  총 이동 거리: 가로 {total_dx}px, 세로 {total_dy}px")

        # 뷰포트 기준 격자 계산 (main.py의 4단계 맞춤선 여백 300, 250 반영)
        vp = page.viewport_size
        usable_w = vp["width"] - 140
        usable_h = vp["height"] - 130
        step_x = usable_w * (1 - config.OVERLAP_RATIO)
        step_y = usable_h * (1 - config.OVERLAP_RATIO)
        zoom_ratio = 1

        cols = math.ceil(abs(total_dx) * zoom_ratio / step_x) + 1
        rows = math.ceil(abs(total_dy) * zoom_ratio / step_y) + 1

        print()
        print("=" * 60)
        print(f" 결과: GRID_ROWS = {rows}, GRID_COLS = {cols}")
        print(f" 총 다운로드: {rows * cols}장")
        print(f' START_URL = "{url}"')
        print("=" * 60)
        print("\n이 값을 config.py에 입력하세요.")

        input("\n브라우저를 닫으려면 Enter...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
