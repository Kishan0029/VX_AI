import os
import cv2
import time
import queue
import threading
import numpy as np
import pythoncom
import win32com.client
import onnxruntime as ort
from PIL import Image
from dotenv import load_dotenv
from google import genai

# Load .env
load_dotenv()

# ==========================================
# 0. COCO Class Dictionary
# ==========================================
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush"
]

# ==========================================
# 1. Non-Blocking Prioritized Audio Worker
# ==========================================
speech_queue = queue.PriorityQueue()
last_spoken = {}
SPEECH_COOLDOWN = 3.2  # seconds

def tts_worker():
    pythoncom.CoInitialize()
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1  # Natural fast pace
    while True:
        try:
            priority, _, text = speech_queue.get()
            if text is None:
                break
            print(f"\n[Earphones] ({'URGENT' if priority==0 else 'GEMINI'}): \"{text}\"", flush=True)
            speaker.Speak(text)
            speech_queue.task_done()
        except Exception as e:
            print(f"[TTS Error]: {e}", flush=True)

threading.Thread(target=tts_worker, daemon=True).start()

def speak_alert(msg, priority=0, key=None):
    now = time.time()
    lookup = key if key else msg
    if lookup in last_spoken and (now - last_spoken[lookup] < SPEECH_COOLDOWN):
        return
    last_spoken[lookup] = now
    
    if priority == 0:
        while not speech_queue.empty():
            try:
                speech_queue.get_nowait()
                speech_queue.task_done()
            except queue.Empty:
                break
    speech_queue.put((priority, now, msg))

# ==========================================
# 2. Asynchronous High-Speed Camera Thread (0ms wait)
# ==========================================
class ThreadedCamera:
    """Eliminates OpenCV capture lag by reading frames in a background thread."""
    def __init__(self, src=0):
        self.cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.ret, self.frame = self.cap.read()
        self.running = True
        self.lock = threading.Lock()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        
    def _capture_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.frame = frame
                    self.ret = ret
            else:
                time.sleep(0.005)
                
    def read(self):
        with self.lock:
            return self.ret, None if self.frame is None else self.frame.copy()
            
    def stop(self):
        self.running = False
        self.cap.release()

# ==========================================
# 3. GPU-Accelerated Models (RTX 3050 DirectML)
# ==========================================
print("[1/4] Initializing Depth Anything V2 on GPU (DirectML)...", flush=True)
depth_opts = ort.SessionOptions()
depth_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
depth_opts.log_severity_level = 3  # Suppress non-critical logs

depth_session = ort.InferenceSession("depth_anything_v2_vits.onnx", depth_opts, providers=['DmlExecutionProvider', 'CPUExecutionProvider'])
print(f"      Active Depth Provider: {depth_session.get_providers()[0]}", flush=True)

mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

depth_lock = threading.Lock()
latest_depth_map = np.zeros((480, 640), dtype=np.uint8)
latest_frame_for_depth = None
is_running = True
depth_hz = 0.0

def depth_worker_thread():
    """Background GPU worker continuously computing depth maps without blocking the camera."""
    global latest_depth_map, depth_hz
    last_time = time.time()
    
    while is_running:
        with depth_lock:
            frame_to_process = None if latest_frame_for_depth is None else latest_frame_for_depth.copy()
                
        if frame_to_process is None:
            time.sleep(0.005)
            continue
            
        h_orig, w_orig = frame_to_process.shape[:2]
        
        # Preprocessing
        img_rgb = cv2.cvtColor(frame_to_process, cv2.COLOR_BGR2RGB)
        img_resized = cv2.resize(img_rgb, (518, 518)).astype(np.float32) / 255.0
        img_norm = (img_resized - mean) / std
        img_input = np.transpose(img_norm, (2, 0, 1))[np.newaxis, ...]
        
        # Inference on GPU
        outputs = depth_session.run(["output"], {"input": img_input})
        depth_raw = outputs[0][0, 0]
        
        # Scale to 0-255
        d_min, d_max = depth_raw.min(), depth_raw.max()
        if d_max > d_min:
            depth_scaled = ((depth_raw - d_min) / (d_max - d_min) * 255.0).astype(np.uint8)
        else:
            depth_scaled = np.zeros_like(depth_raw, dtype=np.uint8)
            
        depth_resized = cv2.resize(depth_scaled, (w_orig, h_orig))
        
        with depth_lock:
            latest_depth_map = depth_resized
            
        now = time.time()
        elapsed = now - last_time
        if elapsed > 0:
            depth_hz = 0.8 * depth_hz + 0.2 * (1.0 / elapsed)
        last_time = now

threading.Thread(target=depth_worker_thread, daemon=True).start()

# Load YOLO26n ONNX on GPU (Runs in ~8ms!)
print("[2/4] Initializing YOLO26n ONNX on GPU (DirectML)...", flush=True)
yolo_opts = ort.SessionOptions()
yolo_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
yolo_opts.log_severity_level = 3

yolo_session = ort.InferenceSession("yolo26n.onnx", yolo_opts, providers=['DmlExecutionProvider', 'CPUExecutionProvider'])
print(f"      Active YOLO Provider: {yolo_session.get_providers()[0]}", flush=True)

# ==========================================
# 4. Multi-Object Tracker (IoU + Depth EMA)
# ==========================================
def compute_iou(box1, box2):
    xA, yA = max(box1[0], box2[0]), max(box1[1], box2[1])
    xB, yB = min(box1[2], box2[2]), min(box1[3], box2[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0

class TrackedObstacle:
    def __init__(self, track_id, label, box, conf, depth_val):
        self.track_id = track_id
        self.label = label
        self.box = box
        self.conf = conf
        self.depth_val = depth_val
        self.hits = 1
        self.time_since_update = 0

class ObstacleTracker:
    def __init__(self, iou_thresh=0.25, max_age=8):
        self.next_id = 1
        self.tracks = []
        self.iou_thresh = iou_thresh
        self.max_age = max_age

    def update(self, detections, depth_map):
        for t in self.tracks:
            t.time_since_update += 1
            
        matched_tracks, matched_dets = set(), set()
        
        if len(self.tracks) > 0 and len(detections) > 0:
            for i, det in enumerate(detections):
                d_label, d_box, d_conf = det
                best_iou, best_idx = 0, -1
                for j, trk in enumerate(self.tracks):
                    if j in matched_tracks or trk.label != d_label:
                        continue
                    iou = compute_iou(d_box, trk.box)
                    if iou > best_iou:
                        best_iou, best_idx = iou, j
                        
                if best_iou > self.iou_thresh and best_idx != -1:
                    matched_tracks.add(best_idx)
                    matched_dets.add(i)
                    t = self.tracks[best_idx]
                    t.box = d_box
                    t.conf = d_conf
                    t.hits += 1
                    t.time_since_update = 0
                    
                    x1, y1, x2, y2 = map(int, d_box)
                    h, w = depth_map.shape[:2]
                    crop = depth_map[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
                    if crop.size > 0:
                        cur_depth = np.median(crop)
                        t.depth_val = 0.65 * t.depth_val + 0.35 * cur_depth

        for i, det in enumerate(detections):
            if i not in matched_dets:
                d_label, d_box, d_conf = det
                x1, y1, x2, y2 = map(int, d_box)
                h, w = depth_map.shape[:2]
                crop = depth_map[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
                cur_d = np.median(crop) if crop.size > 0 else 0
                self.tracks.append(TrackedObstacle(self.next_id, d_label, d_box, d_conf, cur_d))
                self.next_id += 1

        self.tracks = [t for t in self.tracks if t.time_since_update <= self.max_age]
        return [t for t in self.tracks if (t.hits >= 2 or t.conf > 0.48) and t.time_since_update == 0]

obstacle_tracker = ObstacleTracker()

# ==========================================
# 5. Connect Gemini 2.5 Flash Brain
# ==========================================
print("[3/4] Connecting Gemini 2.5 Flash Multimodal Brain...", flush=True)
api_key = os.environ.get("GEMINI_API_KEY")
gemini_client = None
if api_key:
    gemini_client = genai.Client(api_key=api_key)
    print("      Gemini API Connected Successfully!", flush=True)

GEMINI_PROMPT = (
    "You are VX AI, an assistive visual brain for a visually impaired user wearing a chest necklace camera with earphones. "
    "Analyze the image and provide high-level intelligent guidance:\n"
    "1. Describe the key setting (e.g. room type, sidewalk, corridor, intersection).\n"
    "2. Point out significant objects, people's actions, or navigational targets (e.g. open door, empty chair, stairs).\n"
    "3. Read any visible signboards, text, or labels.\n"
    "4. Be concise and natural (maximum 1 to 2 sentences) for immediate earphone delivery."
)

gemini_status_text = "Press SPACE for Gemini Intelligence, or 'A' for Auto Mode"
is_gemini_analyzing = False

def trigger_gemini_async(frame_bgr):
    global is_gemini_analyzing, gemini_status_text
    if gemini_client is None or is_gemini_analyzing:
        return
    is_gemini_analyzing = True
    gemini_status_text = "Gemini is analyzing the scene..."
    
    def worker():
        global is_gemini_analyzing, gemini_status_text
        try:
            rgb_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_frame)
            t0 = time.time()
            
            try:
                response = gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[pil_img, GEMINI_PROMPT]
                )
            except Exception:
                response = gemini_client.models.generate_content(
                    model="gemini-flash-latest",
                    contents=[pil_img, GEMINI_PROMPT]
                )
                
            elapsed = time.time() - t0
            desc = response.text.strip().replace("\n", " ")
            print(f"[Gemini Response ({elapsed:.2f}s)]: {desc}", flush=True)
            gemini_status_text = desc
            speak_alert(desc, priority=1, key="gemini_intel")
        except Exception as e:
            err = f"Gemini error: {str(e)[:80]}"
            print(err, flush=True)
            gemini_status_text = err
        finally:
            is_gemini_analyzing = False

    threading.Thread(target=worker, daemon=True).start()

# ==========================================
# 6. Main High-FPS Master Loop (30-40+ FPS)
# ==========================================
def main():
    global latest_frame_for_depth, is_running
    
    print("[4/4] Starting Threaded Camera Stream...", flush=True)
    cam = ThreadedCamera(0)
    time.sleep(0.5)

    print("\n" + "=" * 65)
    print("  VX AI - High-FPS Real-Time Perception & Gemini Intelligence")
    print("  • GPU Acceleration : YOLO26n ONNX (8ms) + Depth Anything (DirectML)")
    print("  • Target Framerate : 30-45+ FPS Smooth Real-Time Feed")
    print("  Controls:")
    print("    [SPACEBAR] : Ask Gemini to describe the scene & read text")
    print("    [A]        : Toggle Auto Gemini narration (every 7s)")
    print("    [Q / ESC]  : Quit")
    print("=" * 65 + "\n", flush=True)

    speak_alert("VX AI 30 frames per second perception engine ready.", priority=0)

    auto_gemini = False
    last_gemini_auto_time = 0
    GEMINI_INTERVAL = 7.0

    prev_time = time.time()
    fps_smooth = 30.0

    while True:
        ret, frame = cam.read()
        if not ret or frame is None:
            time.sleep(0.005)
            continue

        frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]

        # Feed frame to GPU depth worker and retrieve latest map
        with depth_lock:
            latest_frame_for_depth = frame
            current_depth = latest_depth_map.copy()

        # Framerate tracking
        now = time.time()
        instant_fps = 1.0 / (now - prev_time) if (now - prev_time) > 0 else 30
        prev_time = now
        fps_smooth = 0.9 * fps_smooth + 0.1 * instant_fps

        # Auto-trigger Gemini
        if auto_gemini and (now - last_gemini_auto_time > GEMINI_INTERVAL) and not is_gemini_analyzing:
            last_gemini_auto_time = now
            trigger_gemini_async(frame.copy())

        # Depth visualization (Turbo colormap)
        depth_color = cv2.applyColorMap(current_depth, cv2.COLORMAP_TURBO)

        # Preprocess for GPU YOLO26n ONNX (8ms latency)
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img_resized = cv2.resize(img_rgb, (640, 640)).astype(np.float32) / 255.0
        img_input = np.transpose(img_resized, (2, 0, 1))[np.newaxis, ...]

        # GPU YOLO Inference
        yolo_out = yolo_session.run(None, {'images': img_input})[0][0]  # shape: (300, 6)
        
        # Filter valid detections: [x1, y1, x2, y2, score, class_id]
        scale_x = w / 640.0
        scale_y = h / 640.0
        detections = []
        
        for det in yolo_out:
            conf = float(det[4])
            if conf >= 0.28:
                cls_id = int(det[5])
                label = COCO_CLASSES[cls_id] if cls_id < len(COCO_CLASSES) else f"obj_{cls_id}"
                x1 = int(det[0] * scale_x)
                y1 = int(det[1] * scale_y)
                x2 = int(det[2] * scale_x)
                y2 = int(det[3] * scale_y)
                detections.append((label, [x1, y1, x2, y2], conf))

        # Update persistent tracker with depth map smoothing
        tracked_obstacles = obstacle_tracker.update(detections, current_depth)

        # Walking corridor boundaries (central 40% of screen)
        c_left, c_right = int(w * 0.30), int(w * 0.70)
        cv2.line(frame, (c_left, 0), (c_left, h), (255, 200, 0), 1)
        cv2.line(frame, (c_right, 0), (c_right, h), (255, 200, 0), 1)
        cv2.putText(frame, "WALKING LANE", (c_left + 10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 200, 0), 1)

        highest_alert = None
        max_proximity = 0

        # Hand / Close Barrier Safety Check
        center_roi = current_depth[int(h*0.3):int(h*0.7), int(w*0.35):int(w*0.65)]
        center_depth_val = np.percentile(center_roi, 85) if center_roi.size > 0 else 0

        if center_depth_val > 218:
            highest_alert = ("Caution: Hand or barrier right in front of camera", "central_block", (0, 0, 255))
            max_proximity = center_depth_val

        # Evaluate Tracked Obstacles
        for t in tracked_obstacles:
            x1, y1, x2, y2 = map(int, t.box)
            mid_x = (x1 + x2) / 2.0
            in_corridor = (c_left <= mid_x <= c_right)
            d_val = t.depth_val

            if d_val > 210:
                status = "VERY CLOSE (<1m)"
                color = (0, 0, 255)
                priority = 2
                msg = f"Warning, {t.label} very close"
            elif in_corridor and d_val > 160:
                status = "AHEAD (2m)"
                color = (0, 165, 255)
                priority = 1
                msg = f"{t.label} ahead in your path"
            elif in_corridor:
                status = "AHEAD (>3m)"
                color = (0, 255, 120)
                priority = 0
                msg = None
            else:
                status = "SIDE (SAFE)"
                color = (0, 255, 0)
                priority = 0
                msg = None

            if priority >= 1 and d_val > max_proximity and msg:
                max_proximity = d_val
                highest_alert = (msg, f"{t.label}_{t.track_id}_{status}", color)

            # Draw tracked bounding box and label
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            tag = f"#{t.track_id} {t.label} [{status}]"
            cv2.putText(frame, tag, (x1, max(20, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 2)
            cv2.rectangle(depth_color, (x1, y1), (x2, y2), (255, 255, 255), 1)

        # Trigger safety reflex audio
        if highest_alert:
            msg, key, _ = highest_alert
            speak_alert(msg, priority=0, key=key)

        # Top Information Bar
        cv2.rectangle(frame, (0, 0), (w, 45), (20, 20, 20), -1)
        alert_str = highest_alert[0] if highest_alert else "Path Clear"
        alert_col = highest_alert[2] if highest_alert else (0, 255, 0)
        cv2.putText(frame, f"REFLEX: {alert_str.upper()}", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.60, alert_col, 2)
        cv2.putText(frame, f"CAM: {int(fps_smooth)} FPS", (w - 140, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (0, 255, 0), 1)

        cv2.rectangle(depth_color, (0, 0), (w, 45), (20, 20, 20), -1)
        cv2.putText(depth_color, f"DEPTH (GPU DirectML): {depth_hz:.1f} Hz", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

        # Bottom Intelligence Bar (Gemini Output)
        cv2.rectangle(frame, (0, h - 50), (w, h), (25, 25, 25), -1)
        intel_str = "GEMINI: " + gemini_status_text
        display_gemini = (intel_str[:65] + "...") if len(intel_str) > 65 else intel_str
        cv2.putText(frame, display_gemini, (15, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 220, 255), 1)

        cv2.rectangle(depth_color, (0, h - 50), (w, h), (25, 25, 25), -1)
        auto_label = "AUTO: ON (7s)" if auto_gemini else "AUTO: OFF ('A')"
        cv2.putText(depth_color, f"SPACE: Ask Gemini | {auto_label}", (15, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (200, 200, 200), 1)

        # Combine side-by-side: [RGB Camera + Tracking] | [Depth Map]
        combined = np.hstack((frame, depth_color))
        cv2.imshow("VX AI - Real-Time Reflex + Gemini Intelligence", combined)

        key = cv2.waitKey(1) & 0xFF
        if key == ord(' '):
            trigger_gemini_async(frame.copy())
        elif key == ord('a') or key == ord('A'):
            auto_gemini = not auto_gemini
            state = "enabled" if auto_gemini else "disabled"
            speak_alert(f"Gemini auto narration {state}", priority=1)
        elif key == ord('q') or key == 27:
            break

    is_running = False
    cam.stop()
    cv2.destroyAllWindows()
    speak_alert("VX AI stopped.", priority=0)
    time.sleep(1)
    speech_queue.put((0, 0, None))
    print("[Info] Finished cleanly.")

if __name__ == "__main__":
    main()
