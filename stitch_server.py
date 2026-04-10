"""
이미지 병합 서버 - 고해상도 SCANS 엔진 적용
실행: python3 stitch_server.py
"""
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import cv2
import numpy as np
import io
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

@app.route('/stitch', methods=['POST'])
def stitch():
    files = request.files.getlist('images')
    if len(files) < 2:
        return '병합을 위해 이미지가 2장 이상 필요합니다.', 400

    print(f"[{datetime.now()}] 병합 요청 수신: {len(files)}장")
    
    imgs = []
    for f in files:
        data = np.frombuffer(f.read(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is not None:
            imgs.append(img)
            
    if len(imgs) < 2:
        return '유효한 이미지 파일을 찾을 수 없습니다.', 400

    # SCANS 모드 Stitcher 생성 (평면 스캔 및 고해상도 지도에 최적화)
    stitcher = cv2.Stitcher.create(cv2.Stitcher_SCANS)
    
    # [중요] 고해상도 유지 설정
    # -1로 설정하면 내부 리사이징 없이 원본 해상도에서 분석 및 합성을 수행합니다.
    stitcher.setRegistrationResol(-1)   # 특징점 탐색 해상도
    stitcher.setSeamEstimationResol(-1) # 이음새 보정 해상도
    stitcher.setCompositingResol(-1)    # 최종 결과물 해상도

    try:
        status, result = stitcher.stitch(imgs)
        
        if status != cv2.Stitcher_OK:
            error_map = {
                1: "겹치는 영역이 부족하여 특징점을 찾을 수 없습니다.",
                2: "이미지 간의 기하학적 정렬에 실패했습니다.",
                3: "카메라 파라미터 보정이 불가능합니다."
            }
            msg = error_map.get(status, f"병합 실패 (코드 {status})")
            print(f"병합 오류: {msg}")
            return msg, 400

        # 결과물 인코딩 (PNG, 무손실 압축 옵션)
        _, buf = cv2.imencode('.png', result, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        
        print(f"병합 성공: {result.shape[1]}x{result.shape[0]} 픽셀")
        return send_file(io.BytesIO(buf.tobytes()), mimetype='image/png')

    except Exception as e:
        print(f"서버 내부 오류: {e}")
        return f"서버 오류: {str(e)}", 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "version": "1.1.0", "engine": "OpenCV SCANS"}), 200

if __name__ == '__main__':
    # Flask 앱 실행 (Vite 개발 환경과의 연동을 위해 호스트 지정)
    print("------------------------------------------")
    print("  고해상도 이미지 병합 서버가 시작되었습니다.")
    print("  URL: http://localhost:5001")
    print("------------------------------------------")
    app.run(host='0.0.0.0', port=5001, debug=False)
