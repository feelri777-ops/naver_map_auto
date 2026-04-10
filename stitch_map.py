import os
import json
import cv2
import numpy as np
from PIL import Image
from datetime import datetime

class MapStitcher:
    def __init__(self, download_path=None, file_list=None):
        self.download_path = download_path
        self.file_list = file_list
        self.meta = None
        
        # 파일 리스트가 없고 폴더만 지정된 경우 메타데이터 로드 시도
        if download_path and not file_list:
            self.meta_path = os.path.join(download_path, "metadata.json")
            if os.path.exists(self.meta_path):
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    self.meta = json.load(f)

    def stitch(self):
        """하이브리드 병합 실행: OpenCV(정밀) 시도 후 실패 시 Pillow(좌표기반)로 폴백"""
        try:
            print("1단계: OpenCV SCANS 엔진을 사용한 정밀 병합 시도 중...")
            return self.stitch_cv2()
        except Exception as e:
            print(f"OpenCV 병합 실패 ({e}). 2단계: 좌표 기반(Pillow) 병합으로 전환합니다.")
            return self.stitch_pillow()

    def stitch_cv2(self):
        """cv2.Stitcher_SCANS를 사용한 특징점 기반 정밀 병합"""
        imgs = []
        
        # 입력 소스 결정 (직접 선택한 파일 리스트 vs 폴더 내 자동 목록)
        if self.file_list:
            source_files = self.file_list
        elif self.meta and self.download_path:
            total = self.meta["rows"] * self.meta["cols"]
            source_files = [os.path.join(self.download_path, f"map_{i:03d}.png") for i in range(1, total + 1)]
        else:
            raise ValueError("병합할 파일 리스트 또는 폴더 메타데이터가 없습니다.")

        for file_path in source_files:
            if not os.path.exists(file_path):
                continue
            
            # OpenCV로 이미지 로드
            img = cv2.imread(file_path)
            if img is not None:
                imgs.append(img)
        
        if len(imgs) < 2:
            raise ValueError("병합할 이미지가 충분하지 않습니다.")

        # SCANS 모드 Stitcher 생성 (스캔/문서 병합에 최적화)
        stitcher = cv2.Stitcher.create(cv2.Stitcher_SCANS)
        
        # 해상도 유지 설정 (중요: 해상도 손실 방지)
        stitcher.setRegistrationResol(-1)   # 특징점 탐색 시 원본 해상도 사용
        stitcher.setSeamEstimationResol(-1) # 이음새 추정 시 원본 해상도 사용
        stitcher.setCompositingResol(-1)    # 최종 합성 시 원본 해상도 사용

        status, result = stitcher.stitch(imgs)

        if status != cv2.Stitcher_OK:
            codes = {1: '겹치는 영역 부족', 2: '특징점 추정 실패', 3: '카메라 파라미터 보정 실패'}
            raise RuntimeError(f"OpenCV 병합 오류: {codes.get(status, f'코드 {status}')}")

        # 결과 저장 경로 결정
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"merged_map_cv2_{timestamp}.png"
        
        save_dir = self.download_path if self.download_path else os.path.dirname(source_files[0])
        output_path = os.path.join(save_dir, output_filename)
        
        # 고품질 PNG 저장
        cv2.imwrite(output_path, result, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        return output_path

    def stitch_pillow(self):
        """기존의 좌표 및 Step 기반 무손실 병합 (Fallback용)"""
        rows = self.meta["rows"]
        cols = self.meta["cols"]
        css_step_x = self.meta["css_step_x"]
        css_step_y = self.meta["css_step_y"]
        css_tile_w = self.meta["css_tile_w"]
        css_tile_h = self.meta["css_tile_h"]
        
        first_file = os.path.join(self.download_path, "map_001.png")
        with Image.open(first_file) as first_img:
            real_w, real_h = first_img.size
            dpr = real_w / css_tile_w
            
        pixel_step_x = int(round(css_step_x * dpr))
        pixel_step_y = int(round(css_step_y * dpr))
        
        final_w = pixel_step_x * (cols - 1) + real_w
        final_h = pixel_step_y * (rows - 1) + real_h
        
        canvas = Image.new("RGB", (final_w, final_h), (255, 255, 255))
        
        count = 0
        for r in range(rows):
            for c in range(cols):
                count += 1
                file_path = os.path.join(self.download_path, f"map_{count:03d}.png")
                if not os.path.exists(file_path):
                    continue
                
                with Image.open(file_path) as tile:
                    paste_x = c * pixel_step_x
                    paste_y = r * pixel_step_y
                    canvas.paste(tile, (paste_x, paste_y))
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"merged_map_fallback_{timestamp}.png"
        output_path = os.path.join(self.download_path, output_filename)
        canvas.save(output_path, "PNG", optimize=True)
        return output_path

if __name__ == "__main__":
    import sys
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "./downloads"
    try:
        stitcher = MapStitcher(target_dir)
        print(f"최종 결과물: {stitcher.stitch()}")
    except Exception as e:
        print(f"오류: {e}")
