"""
네이버 지도 실내지도 다운로드 자동화 (GUI 버전)

흐름:
  1. 브라우저 열기 → 건물 검색 → 실내지도 진입 (수동)
  2. 축소 상태에서 좌상단 코너 / 우하단 코너 지정
  3. 자동으로 최대 확대 → 좌상단 복귀 → 격자 촬영
"""

import os
import sys

# macOS Tkinter 호환성 개선을 위한 환경 변수
os.environ['TK_SILENCE_DEPRECATION'] = '1'

import asyncio
import math
import re
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk
import io
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

    if before_zoom and after_zoom:
        if after_zoom > before_zoom:
            ratio = 2 ** (after_zoom - before_zoom)
            if log_fn:
                log_fn(f"줌: {before_zoom:.2f} → {after_zoom:.2f} (x{ratio:.1f})")
            return ratio
        else:
            if log_fn:
                log_fn(f"이미 최대 확대 상태 (줌 레벨: {after_zoom:.2f}) — 비율 x1")
            return 1.0
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
# ============================================================
# 오버레이 JS
# ============================================================
OVERLAY_JS = """
const overlay = document.createElement('div');
overlay.id = '__map_overlay';
overlay.innerHTML = `
    <!-- 경계선 표시 (상/하/좌/우) -->
    <div id="__edge_top" style="position:fixed; left:0; top:0; width:100%; height:3px; background:#00ff88; z-index:99998; pointer-events:none; display:none; box-shadow:0 0 8px #00ff88;"></div>
    <div id="__edge_bottom" style="position:fixed; left:0; bottom:0; width:100%; height:3px; background:#00ff88; z-index:99998; pointer-events:none; display:none; box-shadow:0 0 8px #00ff88;"></div>
    <div id="__edge_left" style="position:fixed; left:0; top:0; width:3px; height:100%; background:#00ff88; z-index:99998; pointer-events:none; display:none; box-shadow:0 0 8px #00ff88;"></div>
    <div id="__edge_right" style="position:fixed; right:0; top:0; width:3px; height:100%; background:#00ff88; z-index:99998; pointer-events:none; display:none; box-shadow:0 0 8px #00ff88;"></div>

    <!-- 경계선 라벨 -->
    <div id="__edge_label_top" style="position:fixed; left:50%; top:6px; transform:translateX(-50%); background:#00ff88; color:#000; padding:2px 10px; border-radius:4px; font-size:12px; font-weight:bold; z-index:99999; pointer-events:none; display:none;">▲ 상단</div>
    <div id="__edge_label_bottom" style="position:fixed; left:50%; bottom:6px; transform:translateX(-50%); background:#00ff88; color:#000; padding:2px 10px; border-radius:4px; font-size:12px; font-weight:bold; z-index:99999; pointer-events:none; display:none;">▼ 하단</div>
    <div id="__edge_label_left" style="position:fixed; left:6px; top:50%; transform:translateY(-50%); background:#00ff88; color:#000; padding:2px 10px; border-radius:4px; font-size:12px; font-weight:bold; z-index:99999; pointer-events:none; display:none;">◀ 좌측</div>
    <div id="__edge_label_right" style="position:fixed; right:6px; top:50%; transform:translateY(-50%); background:#00ff88; color:#000; padding:2px 10px; border-radius:4px; font-size:12px; font-weight:bold; z-index:99999; pointer-events:none; display:none;">▶ 우측</div>

    <!-- 안내 라벨 -->
    <div id="__guide_label" style="position:fixed; left:50%; top:20px; transform:translateX(-50%);
        background:rgba(0,0,0,0.85); color:white; padding:12px 24px; border-radius:30px;
        font-size:16px; font-weight:bold; z-index:99999; pointer-events:none; display:none;
        white-space:nowrap; border: 2px solid #ff4444;"></div>
`;
document.body.appendChild(overlay);

window.__totalDragX = 0;
window.__totalDragY = 0;
window.__tracking = true;

document.addEventListener('mousedown', (e) => {
    window.__lastX = e.clientX;
    window.__lastY = e.clientY;
});

document.addEventListener('mouseup', (e) => {
    if (window.__tracking) {
        window.__totalDragX += (window.__lastX - e.clientX);
        window.__totalDragY += (window.__lastY - e.clientY);
    }
});

// 네이버 지도 UI 요소 숨기기/보이기
window.__toggleNaverUI = function() {
    let style = document.getElementById('__naver_ui_hider');
    if (style) {
        style.remove();
    } else {
        style = document.createElement('style');
        style.id = '__naver_ui_hider';
        style.innerHTML = `
            #header, .search_area,
            .control_container, .copyright_container,
            .entrance_container, .map_logo {
                display: none !important;
            }
        `;
        document.head.appendChild(style);
    }
};
"""


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("네이버 실내지도 다운로드")
        self.root.geometry("1400x900")
        self.root.resizable(False, False)

        self.page = None
        self.context = None
        self.pw = None
        
        # 신규 4방향 경계값
        self.bounds = {'top': None, 'bottom': None, 'left': None, 'right': None}
        
        self.state = "init"
        self.stop_requested = False

        # 이미지 캐시
        self.overview_img = None
        self.overview_photo = None
        self.canvas_width = 900
        self.canvas_height = 800
        self.rect_id = None

        self._build_ui()

        # macOS 백지 현상 방지를 위한 강제 업데이트
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

    def _build_ui(self):
        # 라이트 테마 색상
        self.bg_color = "#f5f5f7"
        self.panel_color = "#ffffff"
        self.accent_color = "#2563eb"
        self.text_color = "#1a1a1a"
        self.secondary_text = "#6b7280"
        self.border_color = "#e5e7eb"
        self.green = "#16a34a"
        self.red = "#dc2626"

        self.root.configure(bg=self.bg_color)

        # ttk 스타일 설정
        style = ttk.Style()
        try:
            # macOS 시스템 테마와 충돌을 최소화하기 위해 clam 또는 alt 사용 시도
            if sys.platform == "darwin":
                style.theme_use("alt") 
            else:
                style.theme_use("clam")
        except Exception:
            pass

        style.configure("Title.TLabel", background=self.bg_color, foreground=self.accent_color,
                         font=("Helvetica", 15, "bold"))
        style.configure("Guide.TLabel", background=self.bg_color, foreground=self.secondary_text,
                         font=("Helvetica", 9))
        style.configure("Status.TLabel", background=self.panel_color, foreground=self.secondary_text,
                         font=("Monaco", 9))
        style.configure("Progress.TLabel", background=self.bg_color, foreground=self.text_color,
                         font=("Helvetica", 9))

        style.configure("Primary.TButton", font=("Helvetica", 9), padding=(12, 8))
        style.map("Primary.TButton",
                   background=[("active", "#1d4ed8"), ("!disabled", self.accent_color), ("disabled", "#d1d5db")],
                   foreground=[("!disabled", "white"), ("disabled", "#9ca3af")])

        style.configure("Green.TButton", font=("Helvetica", 10, "bold"), padding=(12, 10))
        style.map("Green.TButton",
                   background=[("active", "#15803d"), ("!disabled", self.green), ("disabled", "#d1d5db")],
                   foreground=[("!disabled", "white"), ("disabled", "#9ca3af")])

        style.configure("Red.TButton", font=("Helvetica", 9), padding=(12, 8))
        style.map("Red.TButton",
                   background=[("active", "#b91c1c"), ("!disabled", self.red), ("disabled", "#d1d5db")],
                   foreground=[("!disabled", "white"), ("disabled", "#9ca3af")])

        style.configure("Edge.TButton", font=("Helvetica", 9), padding=(8, 6))
        style.map("Edge.TButton",
                   background=[("active", "#e0e7ff"), ("!disabled", "#eef2ff"), ("disabled", "#f3f4f6")],
                   foreground=[("!disabled", "#3730a3"), ("disabled", "#9ca3af")])

        style.configure("Card.TLabelframe", background=self.panel_color, relief="solid", borderwidth=1)
        style.configure("Card.TLabelframe.Label", background=self.panel_color, foreground=self.accent_color,
                         font=("Helvetica", 9, "bold"))

        # 전체 레이아웃
        main_frame = tk.Frame(self.root, bg=self.bg_color)
        main_frame.pack(fill="both", expand=True, padx=16, pady=16)

        left_frame = tk.Frame(main_frame, width=400, bg=self.bg_color)
        left_frame.pack(side="left", fill="y")

        # 미니맵 카드
        right_card = tk.Frame(main_frame, bg=self.panel_color, highlightbackground=self.border_color,
                               highlightthickness=1, padx=8, pady=8)
        right_card.pack(side="right", fill="both", expand=True, padx=(16, 0))

        tk.Label(right_card, text="미니맵", font=("Segoe UI", 10, "bold"),
                 bg=self.panel_color, fg=self.text_color, anchor="w").pack(fill="x", pady=(0, 6))

        self.canvas = tk.Canvas(right_card, width=self.canvas_width, height=self.canvas_height,
                                bg="#e8eaed", highlightthickness=1, highlightbackground=self.border_color)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(325, 300, text="브라우저를 열어주세요",
                                fill="#9ca3af", font=("Segoe UI", 11))

        # --- 왼쪽 컨트롤 ---
        self.status_var = tk.StringVar(value="Naver Map Downloader")
        ttk.Label(left_frame, textvariable=self.status_var, style="Title.TLabel").pack(anchor="w", pady=(0, 2))

        self.guide_var = tk.StringVar(value="브라우저 열기 버튼으로 시작하세요")
        ttk.Label(left_frame, textvariable=self.guide_var, style="Guide.TLabel",
                  wraplength=380).pack(anchor="w", pady=(0, 12))

        # 1. 브라우저 열기
        step1 = tk.Frame(left_frame, bg=self.bg_color)
        step1.pack(fill="x", pady=(0, 8))

        self.btn_open = ttk.Button(step1, text="브라우저 열기", style="Primary.TButton",
                                    command=self._on_open_browser)
        self.btn_open.pack(side="left", padx=(0, 6), fill="x", expand=True)

        self.btn_toggle_ui = ttk.Button(step1, text="UI 숨김/표시", style="Primary.TButton",
                                         command=self._toggle_naver_ui, state="disabled")
        self.btn_toggle_ui.pack(side="left", fill="x", expand=True)

        # 2. 경계 지정 카드
        boundary_frame = ttk.LabelFrame(left_frame, text="  영역 경계 지정  ", style="Card.TLabelframe",
                                         padding=(12, 10))
        boundary_frame.pack(fill="x", pady=(0, 8))

        y_btn_row = tk.Frame(boundary_frame, bg=self.panel_color)
        y_btn_row.pack(fill="x", pady=(0, 4))
        self.btn_top = ttk.Button(y_btn_row, text="상단 ↑", style="Edge.TButton",
                                   command=lambda: self._on_set_edge('top'), state="disabled")
        self.btn_top.pack(side="left", expand=True, padx=(0, 3))
        self.btn_bottom = ttk.Button(y_btn_row, text="하단 ↓", style="Edge.TButton",
                                      command=lambda: self._on_set_edge('bottom'), state="disabled")
        self.btn_bottom.pack(side="left", expand=True, padx=(3, 0))

        x_btn_row = tk.Frame(boundary_frame, bg=self.panel_color)
        x_btn_row.pack(fill="x", pady=(0, 8))
        self.btn_left = ttk.Button(x_btn_row, text="좌측 ←", style="Edge.TButton",
                                    command=lambda: self._on_set_edge('left'), state="disabled")
        self.btn_left.pack(side="left", expand=True, padx=(0, 3))
        self.btn_right = ttk.Button(x_btn_row, text="우측 →", style="Edge.TButton",
                                     command=lambda: self._on_set_edge('right'), state="disabled")
        self.btn_right.pack(side="left", expand=True, padx=(3, 0))

        self.bound_status_var = tk.StringVar(value="T: -  |  B: -  |  L: -  |  R: -")
        ttk.Label(boundary_frame, textvariable=self.bound_status_var, style="Status.TLabel").pack()

        # 3. 촬영 / 초기화
        action_frame = tk.Frame(left_frame, bg=self.bg_color)
        action_frame.pack(fill="x", pady=(0, 6))

        self.btn_start = ttk.Button(action_frame, text="촬영 시작", style="Green.TButton",
                                     command=self._on_start, state="disabled")
        self.btn_start.pack(side="left", padx=(0, 6), fill="x", expand=True)

        self.btn_reset = ttk.Button(action_frame, text="초기화", style="Red.TButton",
                                     command=self._on_reset, state="disabled")
        self.btn_reset.pack(side="left", fill="x", expand=True)

        self.btn_stop = ttk.Button(left_frame, text="촬영 중지", style="Red.TButton",
                                    command=self._on_stop, state="disabled")
        self.btn_stop.pack(fill="x", pady=(0, 8))

        self.progress_var = tk.StringVar(value="")
        ttk.Label(left_frame, textvariable=self.progress_var, style="Progress.TLabel",
                  wraplength=380).pack(anchor="w", pady=(0, 6))

        # 로그
        log_frame = tk.Frame(left_frame, bg=self.border_color, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=10, font=("Consolas", 9),
                                 bg="#fafafa", fg="#374151", relief="flat",
                                 borderwidth=0, padx=8, pady=6, selectbackground="#bfdbfe")
        self.log_text.pack(fill="both", expand=True)

    def _get_loop(self):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def log(self, msg):
        if not self.log_text.winfo_exists(): return
        self.log_text.insert(tk.END, f"[{self._get_loop().time():.1f}] {msg}\n")
        self.log_text.see(tk.END)
        self.root.update()

    # ============================================================
    # 1. 브라우저 열기
    # ============================================================
    def _on_open_browser(self):
        self.btn_open.config(state="disabled")
        self.status_var.set("브라우저 여는 중...")
        self.root.update()
        self._get_loop().run_until_complete(self._open_browser())

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

        await self.page.goto("https://map.naver.com/", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # 오버레이 주입
        await self.page.evaluate(OVERLAY_JS)

        # 안내 라벨 표시
        await self.page.evaluate("""
            const guide = document.getElementById('__guide_label');
            guide.style.display = 'block';
            guide.textContent = '각 경계선을 화면 가장자리에 맞추고 버튼을 누르세요';
        """)

        self.status_var.set("브라우저 준비 완료!")
        self.guide_var.set("지도를 드래그하여 경계를 화면 가장자리에 맞추고 버튼을 누르세요")
        
        # 버튼 활성화
        self.btn_top.config(state="normal")
        self.btn_bottom.config(state="normal")
        self.btn_left.config(state="normal")
        self.btn_right.config(state="normal")
        self.btn_toggle_ui.config(state="normal")
        self.btn_reset.config(state="normal")
        
        self.state = "init"

    def _on_set_edge(self, edge):
        if not self.page: return
        self._get_loop().run_until_complete(self._capture_edge(edge))

    async def _capture_edge(self, edge):
        dx = await self.page.evaluate("window.__totalDragX")
        dy = await self.page.evaluate("window.__totalDragY")

        if edge == 'top': self.bounds['top'] = dy
        elif edge == 'bottom': self.bounds['bottom'] = dy
        elif edge == 'left': self.bounds['left'] = dx
        elif edge == 'right': self.bounds['right'] = dx

        # 브라우저에 경계선 표시
        await self.page.evaluate(f"""
            document.getElementById('__edge_{edge}').style.display = 'block';
            document.getElementById('__edge_label_{edge}').style.display = 'block';
        """)

        self._update_bound_status()
        self.log(f"{edge.upper()} 경계 지정됨: {dx if edge in ['left','right'] else dy}px")

        # 4개 다 설정되었으면 촬영 시작 버튼 활성화
        if all(v is not None for v in self.bounds.values()):
            self.btn_start.config(state="normal")
            self.guide_var.set("영역 지정 완료! [3. 촬영 시작]을 누르세요")

    def _update_bound_status(self):
        t = f"{self.bounds['top']}" if self.bounds['top'] is not None else "-"
        b = f"{self.bounds['bottom']}" if self.bounds['bottom'] is not None else "-"
        l = f"{self.bounds['left']}" if self.bounds['left'] is not None else "-"
        r = f"{self.bounds['right']}" if self.bounds['right'] is not None else "-"
        self.bound_status_var.set(f"T: {t} | B: {b} | L: {l} | R: {r}")

    def _toggle_naver_ui(self):
        if self.page:
            self._get_loop().run_until_complete(self.page.evaluate("window.__toggleNaverUI()"))

    # ============================================================
    # 위치 다시 잡기
    # ============================================================
    def _on_reset(self):
        self.status_var.set("좌표 초기화됨")
        self.bounds = {'top': None, 'bottom': None, 'left': None, 'right': None}
        self._update_bound_status()
        self.btn_start.config(state="disabled")
        self.btn_top.config(state="normal")
        self.btn_bottom.config(state="normal")
        self.btn_left.config(state="normal")
        self.btn_right.config(state="normal")
        self.guide_var.set("지도를 드래그하여 경계를 화면 가장자리에 맞추고 버튼을 누르세요")
        self.log("경계 초기화")

        # 브라우저 경계선 숨기기
        if self.page:
            self._get_loop().run_until_complete(self._reset_overlay())

        # 미니맵 리셋
        self.canvas.delete("all")
        self.canvas.create_text(325, 300, text="브라우저를 열어주세요", fill="#9ca3af", font=("Segoe UI", 11))
        self.overview_img = None

    async def _reset_overlay(self):
        await self.page.evaluate("""
            // 경계선 숨기기
            ['top','bottom','left','right'].forEach(edge => {
                document.getElementById('__edge_' + edge).style.display = 'none';
                document.getElementById('__edge_label_' + edge).style.display = 'none';
            });
            const guide = document.getElementById('__guide_label');
            guide.style.display = 'block';
            guide.textContent = '각 경계선을 화면 가장자리에 맞추고 버튼을 누르세요';
            guide.style.background = 'rgba(0,0,0,0.85)';
        """)

    def _update_mini_map_image(self, screenshot_bytes):
        """캡처한 스크린샷을 미니맵 캔버스에 표시"""
        img = Image.open(io.BytesIO(screenshot_bytes))
        self.overview_img = img
        
        # 비율 유지하며 리사이즈
        w, h = img.size
        ratio = min(self.canvas_width / w, self.canvas_height / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        self.overview_photo = ImageTk.PhotoImage(img_resized)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.overview_photo)
        self.mini_scale = ratio # 실시간 표시를 위한 스케일 저장

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
        self.btn_top.config(state="disabled")
        self.btn_bottom.config(state="disabled")
        self.btn_left.config(state="disabled")
        self.btn_right.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.state = "running"
        self._get_loop().run_until_complete(self._run_download())

    async def _run_download(self):
        page = self.page

        # 오버레이 가이드 숨기기
        await page.evaluate("""
            document.getElementById('__map_overlay').style.display = 'none';
        """)

        # 0) 현재 위치 확인
        cur_dx = await self.page.evaluate("window.__totalDragX")
        cur_dy = await self.page.evaluate("window.__totalDragY")

        # 뷰포트 크기
        vp = page.viewport_size
        half_w = vp["width"] // 2
        half_h = vp["height"] // 2

        # 1) 경계값을 화면 가장자리 기준으로 보정
        # 경계 지정 시 드래그 누적값은 "화면 중앙" 기준이지만,
        # 실제 경계는 화면 가장자리에 맞췄으므로 뷰포트 절반만큼 보정
        edge_top = self.bounds['top'] - half_h
        edge_bottom = self.bounds['bottom'] + half_h
        edge_left = self.bounds['left'] - half_w
        edge_right = self.bounds['right'] + half_w

        left = min(edge_left, edge_right)
        right = max(edge_left, edge_right)
        top = min(edge_top, edge_bottom)
        bottom = max(edge_top, edge_bottom)

        width_px = right - left
        height_px = bottom - top

        # 2) 촬영 시작점(좌상단)으로 이동
        # 첫 촬영의 화면 중앙이 (left + half_w, top + half_h)에 와야 함
        target_x = left + half_w
        target_y = top + half_h
        self.status_var.set("좌상단 시작점으로 이동 중...")
        self.log(f"영역 크기: {width_px}x{height_px}px")

        dist_x = target_x - cur_dx
        dist_y = target_y - cur_dy
        await drag_map(page, dist_x, dist_y)
        await asyncio.sleep(config.PAN_WAIT)

        # 3) 미니맵용 전체 샷 찍기
        screenshot_bytes = await self.page.screenshot()
        self._update_mini_map_image(screenshot_bytes)

        # 4) 최대 확대
        self.status_var.set("최대 확대 중...")
        zoom_ratio = await zoom_to_max(page, log_fn=self.log)

        # 5) 격자 계산 (전체 뷰포트 사용 — 다운로드 이미지는 전체 화면)
        usable_w = vp["width"]
        usable_h = vp["height"]

        step_x = int(usable_w * (1 - config.OVERLAP_RATIO))
        step_y = int(usable_h * (1 - config.OVERLAP_RATIO))

        total_w = int(width_px * zoom_ratio)
        total_h = int(height_px * zoom_ratio)

        cols = max(1, math.ceil((total_w - usable_w) / step_x) + 1)
        rows = max(1, math.ceil((total_h - usable_h) / step_y) + 1)
        total = rows * cols

        self.log(f"확대 후 전체 영역: {total_w}x{total_h}px")
        self.log(f"격자: {rows}행 x {cols}열 = {total}장")

        # 4) 촬영 시작
        self.status_var.set(f"촬영 중... (0/{total})")
        self.root.update()

        success = 0
        direction = 1

        for row in range(rows):
            for col in range(cols):
                if self.stop_requested:
                    self.log(f"=== {success}장 다운로드 후 중지됨 ===")
                    self.status_var.set(f"중지됨! {success}장 다운로드됨")
                    self.btn_stop.config(state="disabled")
                    self.btn_reset.config(state="normal")
                    return
                
                idx = row * cols + col + 1
                self.progress_var.set(f"[{idx}/{total}] row={row}, col={col}")
                self.status_var.set(f"촬영 중... ({idx}/{total})")
                self.root.update()

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
