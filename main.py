"""
네이버 지도 실내지도 다운로드 자동화 (GUI 버전)

흐름:
  1. 브라우저 열기 → 건물 검색 → 실내지도 진입 (수동)
  2. 축소 상태에서 좌상단 코너 / 우하단 코너 지정
  3. 자동으로 최대 확대 → 좌상단 복귀 → 격자 촬영
"""

import asyncio
import os
import math
import re
import tkinter as tk
from tkinter import messagebox
from playwright.async_api import async_playwright
import config


def get_zoom_from_url(url):
    """URL에서 줌 레벨을 추출한다."""
    match = re.search(r'[?&]c=([0-9.]+)', url)
    if match:
        return float(match.group(1))
    return None


async def drag_map(page, dx, dy):
    """마우스 드래그로 맵을 이동한다."""
    vp = page.viewport_size
    cx = vp["width"] // 2
    cy = vp["height"] // 2

    await page.mouse.move(cx, cy)
    await page.mouse.down()
    steps = 10
    for i in range(1, steps + 1):
        await page.mouse.move(
            cx - int(dx * i / steps),
            cy - int(dy * i / steps),
        )
        await asyncio.sleep(0.05)
    await page.mouse.up()
    await asyncio.sleep(config.PAN_WAIT)


async def zoom_to_max(page, log_fn=None):
    """줌 버튼을 반복 클릭해서 최대 확대. 줌 비율 반환."""
    before_zoom = get_zoom_from_url(page.url)

    zoom_selectors = [
        "button[aria-label='확대']",
        "button.btn_zoom_in",
        "[class*='zoom'] [class*='plus']",
        "[class*='zoom_in']",
    ]
    zoom_btn = None
    for selector in zoom_selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                zoom_btn = btn
                break
        except Exception:
            continue

    if zoom_btn:
        for _ in range(30):
            try:
                await zoom_btn.click()
                await asyncio.sleep(0.4)
            except Exception:
                break
        await asyncio.sleep(2)
    else:
        for _ in range(30):
            await page.mouse.wheel(0, -300)
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)

    after_zoom = get_zoom_from_url(page.url)

    if before_zoom and after_zoom and after_zoom > before_zoom:
        ratio = 2 ** (after_zoom - before_zoom)
        if log_fn:
            log_fn(f"줌: {before_zoom:.2f} → {after_zoom:.2f} (x{ratio:.1f})")
        return ratio
    else:
        if log_fn:
            log_fn("줌 레벨 확인 불가 — 기본 비율(x8) 사용")
        return 8.0


async def click_download(page):
    """우측 사이드바의 다운로드 버튼 클릭."""
    btn = page.locator("button.widget_save")
    try:
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await asyncio.sleep(config.DOWNLOAD_WAIT)
            return True
    except Exception:
        return False
    return False


# ============================================================
# 오버레이 JS
# ============================================================
OVERLAY_JS = """
const overlay = document.createElement('div');
overlay.id = '__map_overlay';
overlay.innerHTML = `
    <!-- 좌상단 코너: 안쪽 여백 70px, 빨간 긴 선 -->
    <div id="__corner_tl" style="position:fixed; left:70px; top:90px; z-index:99999; pointer-events:none; display:none;">
        <div style="position:absolute; left:0; top:0; width:2000px; height:3px; background:#ff0000; opacity:0.8;"></div>
        <div style="position:absolute; left:0; top:0; width:3px; height:2000px; background:#ff0000; opacity:0.8;"></div>
        <div style="position:absolute; left:10px; top:10px; background:rgba(200,0,0,0.9); color:white;
            padding:5px 12px; border-radius:4px; font-size:14px; font-weight:bold; white-space:nowrap;">좌상단</div>
    </div>

    <!-- 우하단 코너: 안쪽 여백 70/40px -->
    <div id="__corner_br" style="position:fixed; right:70px; bottom:40px; z-index:99999; pointer-events:none; display:none;">
        <div style="position:absolute; right:0; bottom:0; width:2000px; height:3px; background:#ff0000; opacity:0.8;"></div>
        <div style="position:absolute; right:0; bottom:0; width:3px; height:2000px; background:#ff0000; opacity:0.8;"></div>
        <div style="position:absolute; right:10px; bottom:10px; background:rgba(200,0,0,0.9); color:white;
            padding:5px 12px; border-radius:4px; font-size:14px; font-weight:bold; white-space:nowrap;">우하단</div>
    </div>

    <!-- 안내 라벨 (화면 상단 중앙) -->
    <div id="__guide_label" style="position:fixed; left:50%; top:20px; transform:translateX(-50%);
        background:rgba(0,0,0,0.8); color:white; padding:10px 20px; border-radius:8px;
        font-size:15px; font-weight:bold; z-index:99999; pointer-events:none; display:none;
        white-space:nowrap;"></div>

    <!-- 선택 영역 사각형 -->
    <div id="__area_rect" style="position:fixed; border:3px dashed rgba(255,0,0,0.5);
        background:rgba(255,0,0,0.05); z-index:99998; pointer-events:none; display:none;"></div>

    <!-- 좌상단 추적 마커 (드래그 후에도 위치 추적) -->
    <div id="__mark_tl_dot" style="position:fixed; z-index:99999; pointer-events:none; display:none;">
        <div style="position:absolute; left:0; top:0; width:2000px; height:2px; background:#ff3333; opacity:0.6;"></div>
        <div style="position:absolute; left:0; top:0; width:2px; height:2000px; background:#ff3333; opacity:0.6;"></div>
    </div>
`;
document.body.appendChild(overlay);

// 좌상단 마커의 초기 화면 좌표
window.__markerScreenX = 0;
window.__markerScreenY = 0;

// 마커 실시간 업데이트
window.__updateMarker = function() {
    const dot = document.getElementById('__mark_tl_dot');
    const rect = document.getElementById('__area_rect');
    if (dot && dot.style.display !== 'none') {
        const dx = window.__totalDragX || 0;
        const dy = window.__totalDragY || 0;
        const mx = window.__markerScreenX - dx;
        const my = window.__markerScreenY - dy;
        dot.style.left = mx + 'px';
        dot.style.top = my + 'px';

        // 사각형: 좌상단 마커 ~ 우하단 코너(화면 우하단)
        if (rect.style.display !== 'none') {
            const right = window.innerWidth - 70;
            const bottom = window.innerHeight - 40;
            rect.style.left = mx + 'px';
            rect.style.top = my + 'px';
            rect.style.width = Math.max(0, right - mx) + 'px';
            rect.style.height = Math.max(0, bottom - my) + 'px';
        }
    }
};
setInterval(window.__updateMarker, 100);
"""


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("네이버 실내지도 다운로드")
        self.root.geometry("420x580")
        self.root.resizable(False, False)

        self.page = None
        self.context = None
        self.pw = None
        self.total_dx = 0
        self.total_dy = 0
        self.state = "init"
        self.stop_requested = False

        self._build_ui()

    def _build_ui(self):
        self.status_var = tk.StringVar(value="브라우저를 열어주세요")
        tk.Label(self.root, textvariable=self.status_var, font=("맑은 고딕", 11, "bold"),
                 wraplength=380, justify="center").pack(pady=(15, 10))

        self.guide_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.guide_var, font=("맑은 고딕", 9),
                 wraplength=380, justify="left", fg="#555").pack(pady=(0, 10))

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=5)

        self.btn_open = tk.Button(btn_frame, text="1. 브라우저 열기", width=20, height=2,
                                   command=self._on_open_browser)
        self.btn_open.pack(pady=3)

        self.btn_top_left = tk.Button(btn_frame, text="2. 좌상단 코너 지정", width=20, height=2,
                                       command=self._on_set_top_left, state="disabled")
        self.btn_top_left.pack(pady=3)

        self.btn_bottom_right = tk.Button(btn_frame, text="3. 우하단 코너 지정", width=20, height=2,
                                           command=self._on_set_bottom_right, state="disabled")
        self.btn_bottom_right.pack(pady=3)

        self.btn_reset = tk.Button(btn_frame, text="위치 다시 잡기", width=20, height=2,
                                    command=self._on_reset, state="disabled")
        self.btn_reset.pack(pady=3)

        self.btn_start = tk.Button(btn_frame, text="4. 촬영 시작!", width=20, height=2,
                                    command=self._on_start, state="disabled",
                                    bg="#4CAF50", fg="white", font=("맑은 고딕", 10, "bold"))
        self.btn_start.pack(pady=5)

        self.btn_stop = tk.Button(btn_frame, text="촬영 중지", width=20, height=2,
                                   command=self._on_stop, state="disabled",
                                   bg="#f44336", fg="white", font=("맑은 고딕", 10, "bold"))
        self.btn_stop.pack(pady=5)

        self.progress_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.progress_var, font=("맑은 고딕", 10),
                 wraplength=380, justify="left").pack(pady=5)

        self.log_text = tk.Text(self.root, height=8, width=50, font=("Consolas", 9))
        self.log_text.pack(pady=5, padx=10)

    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.root.update()

    # ============================================================
    # 1. 브라우저 열기
    # ============================================================
    def _on_open_browser(self):
        self.btn_open.config(state="disabled")
        self.status_var.set("브라우저 여는 중...")
        self.root.update()
        asyncio.get_event_loop().run_until_complete(self._open_browser())

    async def _open_browser(self):
        download_path = os.path.abspath(config.DOWNLOAD_DIR)
        os.makedirs(download_path, exist_ok=True)
        user_data_dir = os.path.abspath("./browser_data")
        os.makedirs(user_data_dir, exist_ok=True)

        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
            locale="ko-KR",
            accept_downloads=True,
            downloads_path=download_path,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        self.download_count = 0
        self.download_path = download_path

        async def handle_download(download):
            self.download_count += 1
            filename = f"map_{self.download_count:03d}.png"
            save_path = os.path.join(self.download_path, filename)
            await download.save_as(save_path)
            self.log(f"저장: {filename}")

        self.page.on("download", handle_download)

        await self.page.goto("https://map.naver.com/", wait_until="networkidle")
        await asyncio.sleep(2)

        # 오버레이 주입
        await self.page.evaluate(OVERLAY_JS)

        # 처음부터 좌상단 코너 마커 + 안내 표시
        await self.page.evaluate("""
            document.getElementById('__corner_tl').style.display = 'block';
            const guide = document.getElementById('__guide_label');
            guide.style.display = 'block';
            guide.textContent = '화면 좌상단 코너에 시작점을 맞추세요';
        """)

        self.status_var.set("브라우저 준비 완료!")
        self.guide_var.set("브라우저에서 수동으로:\n"
                           "  1. 건물 검색 → 실내지도 → 원하는 층 선택\n"
                           "  2. 전체 영역이 보이도록 적당히 축소\n"
                           "  3. 화면 좌상단 코너에 시작점을 맞추고 [좌상단 코너 지정]")
        self.btn_top_left.config(state="normal")
        self.state = "init"

    # ============================================================
    # 2. 좌상단 코너 지정
    # ============================================================
    def _on_set_top_left(self):
        self.btn_top_left.config(state="disabled")
        asyncio.get_event_loop().run_until_complete(self._set_top_left())

    async def _set_top_left(self):
        # 좌상단 확정 표시 + 우하단 코너 안내
        await self.page.evaluate("""
            // 좌상단 코너 확정 (어두운 빨간색)
            const tlCorner = document.getElementById('__corner_tl');
            tlCorner.querySelector('div:last-child').textContent = '좌상단 (확정)';
            tlCorner.querySelector('div:last-child').style.background = 'rgba(150,0,0,0.95)';

            // 좌상단 추적 마커 시작
            window.__markerScreenX = 0;  // 좌상단 코너 = (0, 0)
            window.__markerScreenY = 0;
            document.getElementById('__mark_tl_dot').style.display = 'block';
            document.getElementById('__area_rect').style.display = 'block';

            // 우하단 코너 표시
            document.getElementById('__corner_br').style.display = 'block';

            // 안내 변경
            const guide = document.getElementById('__guide_label');
            guide.textContent = '화면 우하단 코너에 끝점을 맞추세요';
        """)

        # 드래그 추적 시작
        await self.page.evaluate("""
            window.__totalDragX = 0;
            window.__totalDragY = 0;
            window.__tracking = true;
            window.__lastX = 0;
            window.__lastY = 0;

            if (!window.__dragListenerAdded) {
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
                window.__dragListenerAdded = true;
            } else {
                window.__totalDragX = 0;
                window.__totalDragY = 0;
                window.__tracking = true;
            }
        """)

        self.status_var.set("좌상단 코너 지정 완료!")
        self.guide_var.set("지도를 드래그해서\n"
                           "화면 우하단 코너에 끝점을 맞추고 [우하단 코너 지정]")
        self.btn_bottom_right.config(state="normal")
        self.btn_reset.config(state="normal")
        self.state = "top_left_set"
        self.log("좌상단 코너 지정됨")

    # ============================================================
    # 3. 우하단 코너 지정
    # ============================================================
    def _on_set_bottom_right(self):
        self.btn_bottom_right.config(state="disabled")
        asyncio.get_event_loop().run_until_complete(self._set_bottom_right())

    async def _set_bottom_right(self):
        await self.page.evaluate("window.__tracking = false;")
        self.total_dx = await self.page.evaluate("window.__totalDragX")
        self.total_dy = await self.page.evaluate("window.__totalDragY")

        # 확정 표시
        await self.page.evaluate("""
            const guide = document.getElementById('__guide_label');
            guide.textContent = '영역 지정 완료! — 촬영 준비 OK';
            guide.style.background = 'rgba(0,150,0,0.9)';

            const brCorner = document.getElementById('__corner_br');
            brCorner.querySelector('div:last-child').textContent = '우하단 (확정)';
            brCorner.querySelector('div:last-child').style.background = 'rgba(150,0,0,0.95)';
        """)

        self.log(f"이동 거리: {abs(self.total_dx)}x{abs(self.total_dy)}px")

        self.status_var.set("우하단 코너 지정 완료!")
        self.guide_var.set("파란 마커(좌상단) ~ 빨간 마커(우하단) 확인\n\n"
                           "맞으면 [촬영 시작!]\n"
                           "틀리면 [위치 다시 잡기]")
        self.btn_start.config(state="normal")
        self.btn_reset.config(state="normal")
        self.state = "bottom_right_set"

    # ============================================================
    # 위치 다시 잡기
    # ============================================================
    def _on_reset(self):
        asyncio.get_event_loop().run_until_complete(self._reset_overlay())

        self.status_var.set("위치를 다시 잡아주세요")
        self.guide_var.set("화면 좌상단 코너에 시작점을 맞추고\n[좌상단 코너 지정]")
        self.btn_top_left.config(state="normal")
        self.btn_bottom_right.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.btn_reset.config(state="disabled")
        self.state = "init"
        self.log("위치 초기화")

    async def _reset_overlay(self):
        await self.page.evaluate("""
            // 좌상단 코너 초기화
            const tl = document.getElementById('__corner_tl');
            tl.style.display = 'block';
            tl.querySelector('div:last-child').textContent = '좌상단';
            tl.querySelector('div:last-child').style.background = 'rgba(255,0,0,0.9)';

            // 나머지 숨기기
            document.getElementById('__corner_br').style.display = 'none';
            document.getElementById('__mark_tl_dot').style.display = 'none';
            document.getElementById('__area_rect').style.display = 'none';

            const guide = document.getElementById('__guide_label');
            guide.textContent = '화면 좌상단 코너에 시작점을 맞추세요';
            guide.style.background = 'rgba(0,0,0,0.8)';
        """)

    # ============================================================
    # 촬영 중지
    # ============================================================
    def _on_stop(self):
        self.stop_requested = True
        self.btn_stop.config(state="disabled")
        self.status_var.set("중지 요청됨... 현재 작업 완료 후 멈춥니다")
        self.log("중지 요청됨")

    # ============================================================
    # 4. 촬영 시작
    # ============================================================
    def _on_start(self):
        self.stop_requested = False
        self.btn_start.config(state="disabled")
        self.btn_reset.config(state="disabled")
        self.btn_top_left.config(state="disabled")
        self.btn_bottom_right.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.state = "running"
        asyncio.get_event_loop().run_until_complete(self._run_download())

    async def _run_download(self):
        page = self.page

        # 오버레이 제거
        await page.evaluate("""
            document.getElementById('__corner_tl').style.display = 'none';
            document.getElementById('__corner_br').style.display = 'none';
            document.getElementById('__mark_tl_dot').style.display = 'none';
            document.getElementById('__area_rect').style.display = 'none';
            document.getElementById('__guide_label').style.display = 'none';
        """)

        # 1) 우하단 → 좌상단으로 복귀 (축소 상태에서)
        self.status_var.set("좌상단으로 복귀 중...")
        self.root.update()
        self.log("좌상단으로 복귀 중...")
        await drag_map(page, -abs(self.total_dx), -abs(self.total_dy))
        await asyncio.sleep(config.PAN_WAIT)

        # 2) 최대 확대 (좌상단 코너 기준으로 확대됨)
        self.status_var.set("최대 확대 중...")
        self.root.update()
        self.log("최대 확대 중...")
        zoom_ratio = await zoom_to_max(page, log_fn=self.log)

        # 3) 격자 계산
        vp = page.viewport_size
        usable_w = vp["width"] - 140
        usable_h = vp["height"] - 130

        step_x = int(usable_w * (1 - config.OVERLAP_RATIO))
        step_y = int(usable_h * (1 - config.OVERLAP_RATIO))

        cols = math.ceil(abs(self.total_dx) * zoom_ratio / step_x) + 1
        rows = math.ceil(abs(self.total_dy) * zoom_ratio / step_y) + 1
        total = rows * cols

        self.log(f"확대 후 전체 영역: {abs(self.total_dx) * zoom_ratio:.0f}x{abs(self.total_dy) * zoom_ratio:.0f}px")
        self.log(f"격자: {rows}행 x {cols}열 = {total}장")

        # 4) 촬영 시작 (좌상단 코너에서 바로 시작 — 보정 불필요)
        self.status_var.set(f"촬영 중... (0/{total})")
        self.root.update()

        success = 0
        direction = 1

        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col + 1
                self.progress_var.set(f"[{idx}/{total}] row={row}, col={col}")
                self.status_var.set(f"촬영 중... ({idx}/{total})")
                self.root.update()

                if self.stop_requested:
                    self.log(f"=== {success}장 다운로드 후 중지됨 ===")
                    self.status_var.set(f"중지됨! {success}장 다운로드됨")
                    self.btn_stop.config(state="disabled")
                    self.btn_reset.config(state="normal")
                    return

                if await click_download(page):
                    success += 1
                    self.log(f"[{idx}/{total}] 다운로드 성공")
                else:
                    self.log(f"[{idx}/{total}] 다운로드 실패!")

                self.root.update()

                if col < cols - 1:
                    dx = step_x * direction
                    await drag_map(page, dx, 0)

            if row < rows - 1:
                await drag_map(page, 0, step_y)
                direction *= -1

        self.status_var.set(f"완료! {success}/{total}장 다운로드됨")
        self.progress_var.set(f"저장 경로: {self.download_path}")
        self.btn_stop.config(state="disabled")
        self.btn_reset.config(state="normal")
        self.log(f"=== 완료! {success}/{total}장 ===")
        messagebox.showinfo("완료", f"{success}/{total}장 다운로드 완료!\n\n저장 경로:\n{self.download_path}")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
