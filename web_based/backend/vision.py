"""Camera feed + AI detection for the search-area phase.

Simulation (TEST_FLAG=1):
    Local webcam, no AI — just enough to test the click-to-fly workflow
    on the bench without a real video/companion link.

Real drone (TEST_FLAG=0):
    RTSP feed (config.RTSP_URL) + YOLOv11s (config.AI_MODEL_PATH) looking
    for tents / mannequins, boxes drawn live on the feed.

This module only produces frames + detections. The GUI is responsible for
drawing them and for turning clicks into flight.nudge_body() calls.
"""
import threading
import time

import cv2
import config

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None


class CameraWorker:
    def __init__(self, mode: str = None, source=None):
        # mode/source let the caller override config.py at runtime (chosen
        # from the website's UI) instead of always using the fixed config
        # defaults. Falling back to config.* preserves the old behavior
        # when nothing is passed in.
        self.mode = mode or config.CAMERA_MODE
        self.use_rtsp = (self.mode == "rtsp")
        if source is not None:
            self.source = source
        else:
            self.source = config.RTSP_URL if self.use_rtsp else config.WEBCAM_INDEX
        self._cap = None
        self._model = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._frame = None       # latest raw BGR frame (numpy array)
        self._detections = []    # [(x1, y1, x2, y2, label, conf), ...]

    # ── lifecycle ────────────────────────────────────────────────
    def start(self) -> None:
        backend = cv2.CAP_FFMPEG if self.use_rtsp else cv2.CAP_ANY
        source = self.source

        print(f"[VISION] mode={self.mode!r} → opening {source}")
        self._cap = cv2.VideoCapture(source, backend)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera source: {source}")

        if self.use_rtsp:
            if YOLO is None:
                print("[VISION] ultralytics not installed — feed only, no AI detection.")
            else:
                try:
                    print(f"[VISION] Loading AI model {config.AI_MODEL_PATH} ...")
                    self._model = YOLO(config.AI_MODEL_PATH)
                    print("[VISION] AI model ready ✓")
                except Exception as e:
                    print(f"[VISION] AI model unavailable ({e}) — continuing with feed only, no AI detection.")
                    self._model = None
        else:
            print("[VISION] Webcam mode — AI disabled (set config.CAMERA_MODE='rtsp' to enable).")

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap:
            self._cap.release()
        self._cap = None
        self._model = None
        print("[VISION] Camera stopped.")

    # ── worker loop ──────────────────────────────────────────────
    def _loop(self) -> None:
        frame_i = 0
        while self._running:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            frame_i += 1

            dets = []
            if self._model is not None:
                if frame_i % config.AI_INFER_EVERY_N == 0:
                    dets = self._infer(frame)
                else:
                    with self._lock:
                        dets = self._detections   # reuse last result between inference frames

            with self._lock:
                self._frame = frame
                self._detections = dets

    def _infer(self, frame):
        try:
            results = self._model.predict(frame, conf=config.AI_CONF_THRESH, verbose=False)
            dets = []
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                    cls_id = int(box.cls[0])
                    label = self._model.names.get(cls_id, str(cls_id))
                    conf = float(box.conf[0])
                    dets.append((x1, y1, x2, y2, label, conf))
            return dets
        except Exception as e:
            print(f"[VISION] Inference error: {e}")
            return []

    # ── access from GUI thread ────────────────────────────────────
    def get_frame(self):
        """Returns (frame_copy, detections) or (None, []) if nothing yet."""
        with self._lock:
            if self._frame is None:
                return None, []
            return self._frame.copy(), list(self._detections)
