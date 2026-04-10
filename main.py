"""
네이버 지도 실내지도 다운로드 자동화 (GUI 버전)

흐름:
  1. 브라우저 열기 → 건물 검색 → 실내지도 진입 (수동)
  2. 축소 상태에서 좌상단 코너 / 우하단 코너 지정
  3. 자동으로 최대 확대 → 좌상단 복귀 → 격자 촬영
"""

import os
import sys
import asyncio
import math
import re
import io
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk
from playwright.async_api import async_playwright
import config

# macOS Tkinter 호환성 개선
os.environ['TK_SILENCE_DEPRECATION'] = '1'


def get_map_state(url):
    """
    URL에서 (longitude, latitude, zoom, floor)를 추출한다.
    일반 지도: ?c=127.1058092,37.3595953,17,0,0,0,dh&f=B1  (7개 파트)
    실내 지도: ?c=20.00,0,0,0,dh                            (5개 파트, 줌만)
    """
    state = {"lng": None, "lat": None, "zoom": None, "floor": None}
    if not url: return state

    c_match = re.search(r'[?&]c=([^&]+)', url)
    if c_match:
        parts = c_match.group(1).split(',')
        try:
            if len(parts) >= 7:
                # 일반 지도: lng, lat, zoom, ...
                state["lng"] = float(parts[0])
                state["lat"] = float(parts[1])
                state["zoom"] = float(parts[2])
            elif len(parts) >= 1:
                # 실내 지도: zoom, tilt, bearing, 0, dh
                state["zoom"] = float(parts[0])
        except ValueError:
            pass

    f_match = re.search(r'[?&]f=([^&]+)', url)
    if f_match:
        state["floor"] = f_match.group(1)

    return state


async def drag_map(page, dx, dy):
    """마우스 드래그로 맵을 지정된 CSS 픽셀만큼 상대적으로 이동한다."""
    if abs(dx) < 0.5 and abs(dy) < 0.5: return # 미세 이동은 무시

    # DPR을 반영한 지도 영역 중앙 (CSS 픽셀)
    dpr = await page.evaluate("window.devicePixelRatio || 1")
    cx = 63 / dpr + (1857 / dpr) / 2
    cy = (1080 / dpr) / 2

    # 이동 시작 전 현재 위치 고정
    await page.mouse.move(cx, cy)
    await page.mouse.down()
    
    # 지도의 관성을 방지하기 위해 단계를 나누어 부드럽게 이동
    steps = 20
    for i in range(1, steps + 1):
        await page.mouse.move(
            cx - int(dx * i / steps),
            cy - int(dy * i / steps),
        )
        await asyncio.sleep(0.01) # 짧은 주기로 정밀 제어
    
    await page.mouse.up()
    # 지도가 완전히 멈출 때까지 대기
    await asyncio.sleep(config.PAN_WAIT)


async def zoom_to_max(page, log_fn=None):
    """줌 버튼을 반복 클릭해서 최대 확대. 줌 비율 반환."""
    state_before = get_map_state(page.url)
    before_zoom = state_before["zoom"]

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
                s_curr = get_map_state(page.url)
                await zoom_btn.click()
                await asyncio.sleep(0.4)
                s_new = get_map_state(page.url)
                if s_curr["zoom"] and s_new["zoom"] and s_new["zoom"] <= s_curr["zoom"]:
                    break
            except Exception:
                break
        await asyncio.sleep(1)
    else:
        for _ in range(30):
            s_curr = get_map_state(page.url)
            await page.mouse.wheel(0, -300)
            await asyncio.sleep(0.3)
            s_new = get_map_state(page.url)
            if s_curr["zoom"] and s_new["zoom"] and s_new["zoom"] <= s_curr["zoom"]:
                break
        await asyncio.sleep(1)

    state_after = get_map_state(page.url)
    after_zoom = state_after["zoom"]

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
# 오버레이 JS (절대 좌표계 개편 버전)
# ============================================================
OVERLAY_JS = """
(function() {
    // 초기 스크롤 상태 저장
    window.__initialScroll = { x: window.scrollX, y: window.scrollY };

    // 스크롤 복원 함수
    window.__restoreScroll = function() {
        window.scrollTo({
            left: window.__initialScroll.x,
            top: window.__initialScroll.y,
            behavior: 'instant'
        });
    };

    // [개선] 지도가 렌더링되는 실제 컨테이너를 동적으로 탐색
    window.__getMapElement = function() {
        return document.getElementById('container') || 
               document.querySelector('.map_viewer') || 
               document.querySelector('[role="region"]') ||
               document.body;
    };

    // [핵심] 문서 기준 절대 좌표 계산 (Scroll Offset 반영)
    window.__getMapRect = function() {
        const dpr = window.devicePixelRatio || 1;
        const el = window.__getMapElement();
        const rect = el.getBoundingClientRect();
        
        // getBoundingClientRect는 뷰포트 상대좌표이므로 scrollY/X를 더해 절대좌표 산출
        return { 
            left: rect.left + window.scrollX,
            top: rect.top + window.scrollY,
            width: rect.width,
            height: rect.height
        };
    };

    window.__updateOverlayPosition = function() {
        const r = window.__getMapRect();
        const lines = document.getElementById('__naver_overlay_lines');
        if (lines) {
            // position: absolute 이므로 문서 절대 좌표를 그대로 적용
            lines.style.top = r.top + 'px';
            lines.style.left = r.left + 'px';
            lines.style.width = r.width + 'px';
            lines.style.height = r.height + 'px';
            
            const dpr = window.devicePixelRatio || 1;
            const w = Math.round(r.width * dpr);
            const h = Math.round(r.height * dpr);
            const label = document.getElementById('__guide_label');
            if (label) {
                label.innerHTML = `<b>경계 지정 모드 (절대 좌표계)</b><br>캡처 영역: ${w} x ${h}<br>스크롤에 관계없이 위치가 고정됩니다.`;
            }
        }
    };

    // --- 고정 경계선 ---
    window.__boundaryPoints = { top: null, bottom: null, left: null, right: null };
    
    window.__setBoundaryLine = function(edge) {
        const r = window.__getMapRect();
        
        window.__boundaryPoints[edge] = {
            dragX: window.__totalDragX,
            dragY: window.__totalDragY,
            absX: (edge === 'left') ? r.left : (edge === 'right' ? r.left + r.width : 0),
            absY: (edge === 'top') ? r.top : (edge === 'bottom' ? r.top + r.height : 0)
        };
        
        let el = document.getElementById('__boundary_line_' + edge);
        if (!el) {
            el = document.createElement('div');
            el.id = '__boundary_line_' + edge;
            el.style.position = 'absolute'; // fixed -> absolute
            el.style.pointerEvents = 'none';
            el.style.zIndex = '999998';
            el.style.backgroundColor = '#007aff';
            el.style.boxShadow = '0 0 5px rgba(0,122,255,0.5)';
            document.body.appendChild(el);
        }
        el.style.display = 'block';
        window.__updateBoundaryPositions();
    };

    window.__updateBoundaryPositions = function() {
        for (let edge in window.__boundaryPoints) {
            const pt = window.__boundaryPoints[edge];
            if (!pt) continue;
            const el = document.getElementById('__boundary_line_' + edge);
            if (!el) continue;
            const dx = window.__totalDragX - pt.dragX;
            const dy = window.__totalDragY - pt.dragY;
            if (edge === 'left' || edge === 'right') {
                el.style.width = '2px';
                el.style.height = document.documentElement.scrollHeight + 'px'; // 전체 길이
                el.style.top = '0';
                el.style.left = (pt.absX - dx) + 'px';
            } else {
                el.style.height = '2px';
                el.style.width = document.documentElement.scrollWidth + 'px'; // 전체 너비
                el.style.left = '0';
                el.style.top = (pt.absY - dy) + 'px';
            }
        }
    };

    // --- 촬영 격자 ---
    window.__gridCells = [];
    
    window.__drawGrid = function(rows, cols, xOffsets, yOffsets) {
        window.__gridCells.forEach(c => {
            const el = document.getElementById('__grid_cell_' + c.id);
            if (el) el.remove();
        });
        window.__gridCells = [];
        
        const r = window.__getMapRect();
        const dpr = window.devicePixelRatio || 1;
        const tileW = r.width;
        const tileH = r.height;
        
        const leftPt = window.__boundaryPoints['left'];
        const topPt  = window.__boundaryPoints['top'];
        if (!leftPt || !topPt) return;

        const baseCenterX = (r.left + r.width/2) - (window.__totalDragX - leftPt.dragX);
        const baseCenterY = (r.top + r.height/2) - (window.__totalDragY - topPt.dragY);
        
        for (let row = 0; row < rows; row++) {
            for (let col = 0; col < cols; col++) {
                const id = row * cols + col + 1;
                const el = document.createElement('div');
                el.id = '__grid_cell_' + id;
                el.style.position = 'absolute'; // fixed -> absolute
                el.style.pointerEvents = 'none';
                el.style.zIndex = '999997';
                el.style.border = '1px dashed rgba(255,255,255,0.4)';
                el.style.backgroundColor = 'rgba(0, 122, 255, 0.15)';
                el.style.boxSizing = 'border-box';
                el.style.display = 'flex';
                el.style.alignItems = 'center';
                el.style.justifyContent = 'center';
                el.style.color = 'white';
                el.style.fontWeight = 'bold';
                el.style.fontSize = '24px';
                el.style.textShadow = '0 0 4px black';
                el.textContent = id;
                
                const xOffset = xOffsets[col];
                const yOffset = yOffsets[row];
                
                el.style.width = tileW + 'px';
                el.style.height = tileH + 'px';
                el.style.left = (baseCenterX + xOffset - (tileW / 2)) + 'px';
                el.style.top = (baseCenterY + yOffset - (tileH / 2)) + 'px';
                
                document.body.appendChild(el);
                window.__gridCells.push({
                    id: id, 
                    dragX: window.__totalDragX, 
                    dragY: window.__totalDragY,
                    initialLeft: parseFloat(el.style.left), 
                    initialTop: parseFloat(el.style.top)
                });
            }
        }
    };

    window.__updateGridPositions = function() {
        window.__gridCells.forEach(cell => {
            const el = document.getElementById('__grid_cell_' + cell.id);
            if (el) {
                const dx = window.__totalDragX - cell.dragX;
                const dy = window.__totalDragY - cell.dragY;
                el.style.left = (cell.initialLeft - dx) + 'px';
                el.style.top = (cell.initialTop - dy) + 'px';
            }
        });
    };

    window.__highlightGrid = function(activeId) {
        window.__gridCells.forEach(cell => {
            const el = document.getElementById('__grid_cell_' + cell.id);
            if (el) {
                if (cell.id === activeId) {
                    el.style.backgroundColor = 'rgba(255, 235, 59, 0.3)';
                    el.style.border = '3px solid #ffeb3b';
                } else if (cell.id < activeId) {
                    el.style.backgroundColor = 'rgba(76, 175, 80, 0.05)';
                    el.style.border = '1px solid rgba(255,255,255,0.1)';
                } else {
                    el.style.backgroundColor = 'rgba(0, 122, 255, 0.1)';
                    el.style.border = '1px dashed rgba(255,255,255,0.3)';
                }
            }
        });
    };

    window.__getEdgePos = function(edge) {
        const r = window.__getMapRect();
        let absX = 0, absY = 0;
        if (edge === 'left') absX = r.left;
        else if (edge === 'right') absX = r.left + r.width;
        else if (edge === 'top') absY = r.top;
        else if (edge === 'bottom') absY = r.top + r.height;

        return {
            dragX: window.__totalDragX,
            dragY: window.__totalDragY,
            absX: absX,
            absY: absY
        };
    };

    const overlay = document.createElement('div');
    overlay.id = '__naver_overlay';
    overlay.innerHTML = `
        <div id="__naver_overlay_lines" style="position:absolute; pointer-events:none; z-index:999999; border:3px solid #ff4444; opacity:0.7; box-sizing:border-box;"></div>
        <div id="__guide_label" style="position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
                    background:rgba(0,0,0,0.85); color:white; padding:12px 24px;
                    border-radius:30px; font-size:14px; pointer-events:none; z-index:999999;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.5); text-align:center;">
            <b>지보 정밀 좌표 모드</b><br>스크롤 보정 로직이 적용되었습니다.
        </div>
    `;
    document.body.appendChild(overlay);
    
    setInterval(() => {
        window.__updateOverlayPosition();
        window.__updateBoundaryPositions();
        window.__updateGridPositions();
    }, 30);

    // 좌표 정보 및 드래그 추적
    window.__totalDragX = 0;
    window.__totalDragY = 0;
    window.__lastX = 0;
    window.__lastY = 0;
    window.__tracking = false;

    document.addEventListener('mousedown', (e) => {
        window.__tracking = true;
        window.__lastX = e.clientX;
        window.__lastY = e.clientY;
    });

    document.addEventListener('mousemove', (e) => {
        if (window.__tracking) {
            window.__totalDragX += (window.__lastX - e.clientX);
            window.__totalDragY += (window.__lastY - e.clientY);
            window.__lastX = e.clientX;
            window.__lastY = e.clientY;
        }
    });

    document.addEventListener('mouseup', () => {
        window.__tracking = false;
    });
})();
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
        
        # 신규 4방향 정밀 경계 데이터 (경위도 및 드래그 기록 포함)
        self.bounds = {
            'top': None,    # {'lat': val, 'y': dragY}
            'bottom': None, # {'lat': val, 'y': dragY}
            'left': None,   # {'lng': val, 'x': dragX}
            'right': None   # {'lng': val, 'x': dragX}
        }
        
        self.state = "init"
        self.stop_requested = False

        # 이미지 캐시
        self.overview_img = None
        self.overview_photo = None
        self.canvas_width = 900
        self.canvas_height = 800
        self.rect_id = None

        # 썸네일 캐시
        self.thumb_photos = {} # {edge: PhotoImage}
        self.thumb_canvases = {} # {edge: Canvas}

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
                                         command=self._on_naver_ui_toggle, state="disabled")
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

        # --- 썸네일 4개 격자 추가 ---
        self.thumb_frame = tk.Frame(boundary_frame, bg=self.panel_color)
        self.thumb_frame.pack(fill="x", pady=(10, 0))

        edges = [('top', '상단 ↑'), ('bottom', '하단 ↓'), ('left', '좌측 ←'), ('right', '우측 →')]
        for i, (key, label) in enumerate(edges):
            r, c = i // 2, i % 2
            cell = tk.Frame(self.thumb_frame, bg=self.panel_color)
            cell.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
            
            tk.Label(cell, text=label, font=("Helvetica", 8), bg=self.panel_color, fg=self.secondary_text).pack()
            
            canvas = tk.Canvas(cell, width=160, height=100, bg="#f3f4f6", 
                               highlightthickness=1, highlightbackground=self.border_color)
            canvas.pack()
            self.thumb_canvases[key] = canvas
            canvas.create_text(80, 50, text="-", fill="#d1d5db")

        self.thumb_frame.columnconfigure(0, weight=1)
        self.thumb_frame.columnconfigure(1, weight=1)

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
        """백그라운드 스레드에서 관리되는 이벤트 루프 반환"""
        if not hasattr(self, 'loop'):
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self._run_async_loop, daemon=True).start()
        return self.loop

    def _run_async_loop(self):
        """별도 스레드에서 무한 루프 실행"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_coro(self, coro):
        """코루틴을 백그라운드 루프에 안전하게 등록 (예외 시 로그 출력)"""
        future = asyncio.run_coroutine_threadsafe(coro, self._get_loop())
        def _on_done(f):
            exc = f.exception()
            if exc:
                self.root.after(0, lambda: self.log(f"오류: {exc}"))
        future.add_done_callback(_on_done)
        return future

    def log(self, msg):
        """스레드 안전한 로그 기록 (tk.after 사용)"""
        if not self.log_text.winfo_exists(): return
        def _append():
            self.log_text.insert(tk.END, f"[{self.loop.time():.1f}] {msg}\n")
            self.log_text.see(tk.END)
        self.root.after(0, _append)

    # ============================================================
    # 1. 브라우저 열기
    # ============================================================
    def _on_open_browser(self):
        self.btn_open.config(state="disabled")
        self.status_var.set("브라우저 여는 중...")
        self._run_coro(self._open_browser())

    async def _open_browser(self):
        download_path = os.path.abspath(config.DOWNLOAD_DIR)
        os.makedirs(download_path, exist_ok=True)
        user_data_dir = os.path.abspath("./browser_data")
        os.makedirs(user_data_dir, exist_ok=True)

        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--start-maximized", 
                "--disable-infobars",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
            ignore_default_args=["enable-automation"],
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
            guide.innerHTML = '<b>경계 지정 모드</b><br>화면 끝에 건물 경계가 오도록 맞추고 버튼을 누르세요';
        """)

        self.status_var.set("브라우저 준비 완료!")
        self.guide_var.set("화면 끝에 건물 경계가 오도록 맞추고 해당 방향 버튼을 누르세요")
        
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
        self._run_coro(self._capture_edge(edge))

    async def _capture_edge(self, edge):
        # [추가] 중요 좌표 캡처 전 스크롤 상태 강제 복원 (정밀도 확보)
        await self.page.evaluate("if(window.__restoreScroll) window.__restoreScroll();")
        await asyncio.sleep(0.5)

        # 모든 좌표를 CSS 픽셀 단위로 통일 (DPR 변환 없음)
        dx = await self.page.evaluate("window.__totalDragX")
        dy = await self.page.evaluate("window.__totalDragY")
        res = await self.page.evaluate(f"window.__getEdgePos('{edge}')")

        # 맵 영역 크기 (absX/Y 반영)
        rect = await self.page.evaluate("window.__getMapRect()")
        map_w = rect['width']
        map_h = rect['height']
        half_w = map_w / 2
        half_h = map_h / 2

        if edge == 'top':
            self.bounds[edge] = {'center': dy, 'edge': dy - half_h, 'absY': res['absY']}
        elif edge == 'bottom':
            self.bounds[edge] = {'center': dy, 'edge': dy + half_h, 'absY': res['absY']}
        elif edge == 'left':
            self.bounds[edge] = {'center': dx, 'edge': dx - half_w, 'absX': res['absX']}
        elif edge == 'right':
            self.bounds[edge] = {'center': dx, 'edge': dx + half_w, 'absX': res['absX']}

        # 미니맵 및 해당 썸네일 업데이트
        screenshot_bytes = await self.page.screenshot()
        self._ui(lambda: self._update_mini_map_image(screenshot_bytes, edge))

        # 시각적 경계선 추가
        await self.page.evaluate(f"window.__setBoundaryLine('{edge}')")

        self._ui(lambda: self._update_bound_status())
        self.log(f"{edge.upper()} 경계 지정됨 (문서 절대 좌표 기준)")

        # 모든 경계가 지정되었다면 격자 미리보기 생성
        if all(v is not None for v in self.bounds.values()):
            # bounds는 모두 CSS 픽셀 단위
            rect = await self.page.evaluate("window.__getMapRect()")
            map_w_css = rect['width']
            map_h_css = rect['height']

            dist_x = abs(self.bounds['right']['edge'] - self.bounds['left']['edge'])
            dist_y = abs(self.bounds['bottom']['edge'] - self.bounds['top']['edge'])

            # Helper 메서드를 이용해 균등 격자 설정 최적 계산
            cols, x_coords, x_step = self._get_grid_config(
                dist_x, map_w_css, self.bounds['left']['center'], self.bounds['right']['center']
            )
            rows, y_coords, y_step = self._get_grid_config(
                dist_y, map_h_css, self.bounds['top']['center'], self.bounds['bottom']['center']
            )

            # JS에 전달할 때는 반올림된 오프셋 사용 (시작 center 기준 상대 좌표)
            x_offsets = [round(c - self.bounds['left']['center'], 2) for c in x_coords]
            y_offsets = [round(c - self.bounds['top']['center'], 2) for c in y_coords]

            await self.page.evaluate(f"window.__drawGrid({rows}, {cols}, {x_offsets}, {y_offsets})")
            self.log(f"격자 생성: {rows}행 x {cols}열 (균등 오버랩 배정 완료)")

        # 4개 다 설정되었으면 촬영 시작 버튼 활성화
        if all(v is not None for v in self.bounds.values()):
            self._ui(lambda: self.btn_start.config(state="normal"))
            self._ui(lambda: self.guide_var.set("경계 설정 완료! [촬영 시작]을 누르세요"))

    def _get_grid_config(self, total_area_dist, map_size, start_center, end_center):
        """균등 오버랩 배치를 위한 타일 개수 및 정밀 좌표 리스트를 산출한다."""
        import math
        max_step = map_size * 0.8
        # 최소 필요한 타일 수 계산
        count = math.ceil((total_area_dist - map_size) / max_step) + 1 if total_area_dist > map_size else 1
        
        # 시작 중심점부터 끝 중심점까지의 총 이동 거리
        move_dist = end_center - start_center
        # 실제 적용될 균등 간격 (Overlap 보장)
        step = move_dist / (count - 1) if count > 1 else 0
        # 순회할 모든 피봇(Center) 좌표 생성
        coords = [start_center + (step * i) for i in range(count)]
        
        # 부동소수점 오차 보정: 마지막 좌표를 end_center에 강제 스냅
        if count > 1:
            coords[-1] = end_center
        
        return count, coords, step

    def _update_bound_status(self):
        def _fmt(b):
            # 중심점(center) 좌표를 출력하도록 수정
            return f"({b['center']:.0f})" if b else "-"
        self.bound_status_var.set(
            f"T: {_fmt(self.bounds['top'])} | B: {_fmt(self.bounds['bottom'])} | "
            f"L: {_fmt(self.bounds['left'])} | R: {_fmt(self.bounds['right'])}"
        )

    # 층 정보 토글
    def _on_naver_ui_toggle(self):
        if self.page:
            self._run_coro(self.page.evaluate("window.__toggleNaverUI()"))

    async def _drag_long(self, page, dx, dy):
        """장거리 드래그를 여러 조각으로 나누어 안전하게 수행한다."""
        max_step = 500 # 한 번에 드래그할 최대 픽셀
        rem_x = dx
        rem_y = dy
        
        while abs(rem_x) > 1 or abs(rem_y) > 1:
            step_x = max(-max_step, min(max_step, rem_x))
            step_y = max(-max_step, min(max_step, rem_y))
            
            # drag_map은 전역 함수이므로 그대로 호출
            await drag_map(page, step_x, step_y)
            
            rem_x -= step_x
            rem_y -= step_y
            await asyncio.sleep(0.1)

    # ============================================================
    # 위치 다시 잡기
    # ============================================================
    def _on_reset(self):
        """모든 경계 데이터 및 썸네일 초기화"""
        for k in self.bounds: self.bounds[k] = None
        self.bound_status_var.set("T: -  |  B: -  |  L: -  |  R: -")
        self.log("경계 정보를 모두 초기화했습니다.")
        
        # 썸네일 초기화
        for key, canvas in self.thumb_canvases.items():
            canvas.delete("all")
            canvas.create_text(80, 50, text="-", fill="#d1d5db")
        self.thumb_photos.clear()
        
        # 촬영 시작 버튼 비활성화
        self.btn_start.configure(state="disabled")
        if self.page:
            self._run_coro(self._reset_overlay())
        self.log("초기화됨")
        self.btn_top.config(state="normal")
        self.btn_bottom.config(state="normal")
        self.btn_left.config(state="normal")
        self.btn_right.config(state="normal")
        self.guide_var.set("화면 끝에 건물 경계가 오도록 맞추고 해당 방향 버튼을 누르세요")
        self.log("경계 초기화")

        # 미니맵 리셋
        self.canvas.delete("all")
        self.canvas.create_text(325, 300, text="브라우저를 열어주세요", fill="#9ca3af", font=("Segoe UI", 11))
        self.overview_img = None

    async def _reset_overlay(self):
        await self.page.evaluate("""
            // 경계선 및 텍스트 숨기기
            ['top','bottom','left','right'].forEach(edge => {
                const guide = document.getElementById('__edge_' + edge);
                if (guide) guide.style.display = 'none';
                const label = document.getElementById('__edge_label_' + edge);
                if (label) label.style.display = 'none';
                
                // 파란색 시각적 경계선 숨기기
                const line = document.getElementById('__boundary_line_' + edge);
                if (line) line.style.display = 'none';
                window.__boundaryPoints[edge] = null;
            });
            const guide = document.getElementById('__guide_label');
            if (guide) {
                guide.style.display = 'block';
                guide.textContent = '각 경계선을 화면 가장자리에 맞추고 버튼을 누르세요';
                guide.style.background = 'rgba(0,0,0,0.85)';
            }
        """)

    def _update_mini_map_image(self, screenshot_bytes, edge=None):
        """미니맵 및 방향별 썸네일 업데이트"""
        img = Image.open(io.BytesIO(screenshot_bytes))
        w, h = img.size

        # 캔버스의 실제 표시 크기를 사용 (레이아웃 후 크기)
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10: cw = self.canvas_width
        if ch < 10: ch = self.canvas_height

        # 메인 미니맵 업데이트
        ratio = min(cw / w, ch / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        self.overview_img = img
        self.overview_photo = ImageTk.PhotoImage(resized)

        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, anchor="center", image=self.overview_photo)

        # 현재 촬영 완료된 모든 경계선 그리기 (미완성)
        self._draw_bounds_on_canvas()

        # 특정 방향 썸네일 업데이트
        if edge and edge in self.thumb_canvases:
            t_w, t_h = 160, 100
            t_ratio = min(t_w / w, t_h / h)
            t_new_w, t_new_h = int(w * t_ratio), int(h * t_ratio)
            t_resized = img.resize((t_new_w, t_new_h), Image.Resampling.LANCZOS)
            
            photo = ImageTk.PhotoImage(t_resized)
            self.thumb_photos[edge] = photo # 참조 유지
            
            canvas = self.thumb_canvases[edge]
            canvas.delete("all")
            canvas.create_image(t_w//2, t_h//2, anchor="center", image=photo)

    def _draw_bounds_on_canvas(self):
        # (기존 미니맵 위에 빨간 점이나 선을 그리는 로직 - 필요시 추가 가능)
        pass

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
        self._run_coro(self._run_download())

    def _ui(self, fn):
        """Tkinter 작업을 메인 스레드에서 안전하게 실행"""
        self.root.after(0, fn)

    async def _run_download(self):
        page = self.page

        try:
            # 오버레이 가이드 숨기기
            await page.evaluate("""
                var el = document.getElementById('__naver_overlay');
                if (el) el.style.display = 'none';
            """)

            # 1) 미니맵용 스크린샷 (현재 축소 상태에서)
            screenshot_bytes = await page.screenshot()
            self._ui(lambda: self._update_mini_map_image(screenshot_bytes))

            # 2) 경계 간 물리 거리 계산 (미리 보정되어 저장된 경계값의 차이)
            rect = await page.evaluate("window.__getMapRect()")
            dpr = await page.evaluate("window.devicePixelRatio || 1")
            m_w = rect['width'] * dpr
            m_h = rect['height'] * dpr

            # 모든 좌표는 CSS 픽셀 단위 (totalDragX/Y와 동일 단위)
            dist_x = abs(self.bounds['right']['edge'] - self.bounds['left']['edge'])
            dist_y = abs(self.bounds['bottom']['edge'] - self.bounds['top']['edge'])
            self.log(f"전체 촬영 범위: {dist_x:.0f} x {dist_y:.0f} CSS px")

            # 맵 영역 크기 (CSS 픽셀)
            map_w_css = rect['width']   # 1857/dpr
            map_h_css = rect['height']  # 1080/dpr

            # 촬영 시작점 = LEFT center의 X좌표 + TOP center의 Y좌표
            start_x = self.bounds['left']['center']
            start_y = self.bounds['top']['center']

            # 시작점으로 이동
            cur_dx = await page.evaluate("window.__totalDragX")
            cur_dy = await page.evaluate("window.__totalDragY")
            gap_x = start_x - cur_dx
            gap_y = start_y - cur_dy

            self.log(f"촬영 시작점 이동: dx={gap_x:.0f}px, dy={gap_y:.0f}px (CSS)")
            await self._drag_long(page, gap_x, gap_y)
            await asyncio.sleep(1)

            # 격자 계산 및 균등 좌표 산출 (통합 Helper 사용)
            cols, target_x_coords, x_step = self._get_grid_config(
                dist_x, map_w_css, self.bounds['left']['center'], self.bounds['right']['center']
            )
            rows, target_y_coords, y_step = self._get_grid_config(
                dist_y, map_h_css, self.bounds['top']['center'], self.bounds['bottom']['center']
            )
            total = rows * cols

            self.log(f"격자: {rows}행 x {cols}열 = {total}장 (균등 배분 모드)")
            self.log(f"평균 이동 간격: X={x_step:.1f}px, Y={y_step:.1f}px (Overlap: {((1 - abs(x_step)/map_w_css)*100 if map_w_css else 0):.1f}%)")

            # 격자 촬영 시작 (좌표 리스트 순회)
            self._ui(lambda: self.status_var.set(f"촬영 중... (0/{total})"))

            success = 0

            for r_idx, target_y in enumerate(target_y_coords):
                for c_idx, target_x in enumerate(target_x_coords):
                    if self.stop_requested:
                        self.log(f"=== {success}장 다운로드 후 중지됨 ===")
                        self._ui(lambda: self.status_var.set(f"중지됨! {success}장 다운로드됨"))
                        self._ui(lambda: self.btn_stop.config(state="disabled"))
                        self._ui(lambda: self.btn_reset.config(state="normal"))
                        return

                    # 현재 위치 읽기 및 오차 보정 이동
                    current_x = await page.evaluate("window.__totalDragX")
                    current_y = await page.evaluate("window.__totalDragY")
                    move_dx = target_x - current_x
                    move_dy = target_y - current_y

                    if abs(move_dx) > 0.5 or abs(move_dy) > 0.5:
                        await drag_map(page, move_dx, move_dy)
                        await asyncio.sleep(0.7) # 이동 후 타일 로딩 및 안정화 대기

                    # 촬영 UI 업데이트 및 브라우저 하이라이트
                    idx = r_idx * cols + c_idx + 1
                    await page.evaluate(f"window.__highlightGrid({idx})")
                    
                    self._ui(lambda i=idx: self.progress_var.set(f"[{i}/{total}] 행:{r_idx+1}, 열:{c_idx+1}"))
                    self._ui(lambda i=idx: self.status_var.set(f"촬영 중... ({i}/{total})"))

                    # 실제 다운로드 실행
                    if await click_download(page):
                        success += 1
                        self.log(f"[{idx}/{total}] 저장 성공 (X:{target_x:.0f}, Y:{target_y:.0f})")
                    else:
                        self.log(f"[{idx}/{total}] 저장 실패!")
                        # 실패 시 재시도 로직을 넣거나 표시

            s = success
            t = total
            dp = self.download_path
            self._ui(lambda: self.status_var.set(f"완료! {s}/{t}장 다운로드됨"))
            self._ui(lambda: self.progress_var.set(f"저장 경로: {dp}"))
            self._ui(lambda: self.btn_stop.config(state="disabled"))
            self._ui(lambda: self.btn_reset.config(state="normal"))
            self.log(f"=== 완료! {success}/{total}장 ===")
            self._ui(lambda: messagebox.showinfo("완료", f"{s}/{t}장 다운로드 완료!\n\n저장 경로:\n{dp}"))

        except Exception as e:
            self.log(f"촬영 오류: {e}")
            self._ui(lambda: self.status_var.set("오류 발생! 로그를 확인하세요"))
            self._ui(lambda: self.btn_stop.config(state="disabled"))
            self._ui(lambda: self.btn_reset.config(state="normal"))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
