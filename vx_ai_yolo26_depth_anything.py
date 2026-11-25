import cv2
import time
import queue
import threading
import numpy as np
import pythoncom
import win32com.client
import onnxruntime as ort
from ultralytics import YOLO

# ==========================================
# 1. Non-Blocking Audio Speech Worker
# ==========================================
speech_queue = queue.Queue()
last_spoken = {}
SPEECH_COOLDOWN = 3.5  # seconds

def tts_worker():
    pythoncom.CoInitialize()
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1
    while True:
        text = speech_queue.get()
        if text is None:
            break
        try:
            print(f"[Earphone Alert]: \"{text}\"", flush=True)
            speaker.Speak(text)
        except Exception as e:
            print(f"[TTS Error]: {e}", flush=True)
        speech_queue.task_done()

threading.Thread(target=tts_worker, daemon=True).start()

def speak_alert(msg, key=None):
    now = time.time()
    lookup = key if key else msg
    if lookup in last_spoken and (now - last_spoken[lookup] < SPEECH_COOLDOWN):
        return
    last_spoken[lookup] = now
    while speech_queue.qsize() > 1:
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except queue.Empty:
            break
    speech_queue.put(msg)

# ==========================================
# 2. Optimized Depth Anything V2 Background Worker
# ==========================================
print("[1/2] Loading Depth Anything V2 (ONNX)...", flush=True)
session_options = ort.SessionOptions()
session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
depth_session = ort.InferenceSession("depth_anything_v2_vits.onnx", session_options, providers=['CPUExecutionProvider'])

mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Shared depth state
depth_lock = threading.Lock()
latest_depth_map = np.zeros((480, 640), dtype=np.uint8)
latest_frame_for_depth = None
is_running = True
depth_hz = 0.0

def depth_worker_thread():
    """Background worker that continuously computes depth without blocking camera FPS."""
    global latest_depth_map, depth_hz
    last_time = time.time()
    
    while is_running:
        with depth_lock:
            if latest_frame_for_depth is None:
                frame_to_process = None
            else:
                frame_to_process = latest_frame_for_depth.copy()
                
        if frame_to_process is None:
            time.sleep(0.01)
            continue
            
        h_orig, w_orig = frame_to_process.shape[:2]
        
        # Preprocess
        img_rgb = cv2.cvtColor(frame_to_process, cv2.COLOR_BGR2RGB)
        img_resized = cv2.resize(img_rgb, (518, 518)).astype(np.float32) / 255.0
        img_norm = (img_resized - mean) / std
        img_input = np.transpose(img_norm, (2, 0, 1))[np.newaxis, ...]
        
        # Inference
        outputs = depth_session.run(["output"], {"input": img_input})
        depth_raw = outputs[0][0, 0]
        
        # Normalize to 0-255 (Higher = Closer)
        d_min, d_max = depth_raw.min(), depth_raw.max()
        if d_max > d_min:
            depth_scaled = ((depth_raw - d_min) / (d_max - d_min) * 255.0).astype(np.uint8)
        else:
            depth_scaled = np.zeros_like(depth_raw, dtype=np.uint8)
            
        depth_resized = cv2.resize(depth_scaled, (w_orig, h_orig))
        
        # Update shared buffer atomically
        with depth_lock:
            latest_depth_map = depth_resized
            
        now = time.time()
        elapsed = now - last_time
        if elapsed > 0:
            depth_hz = 1.0 / elapsed
        last_time = now

threading.Thread(target=depth_worker_thread, daemon=True).start()

# ==========================================
# 3. Load YOLO26n
# ==========================================
print("[2/2] Loading YOLO26n Nano Model...", flush=True)
yolo_model = YOLO("yolo26n.pt")
print("[Ready] Dual-Engine Real-Time Pipeline Ready!\n", flush=True)

# ==========================================
# 4. Main Real-Time 30+ FPS Loop
# ==========================================
def main():
    global latest_frame_for_depth, is_running
    
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[Error] Could not open webcam.")
        return
        
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("=" * 65)
    print("  VX AI - High-FPS Real-Time Perception")
    print("  • Camera & YOLO26n: Running at full 30+ FPS (Zero Lag)")
    print("  • Depth Anything V2: Continuously updating in background")
    print("  Press 'q' or 'ESC' to quit")
    print("=" * 65, flush=True)

    speak_alert("VX AI high framerate real time perception started.")

    prev_time = time.time()
    fps_smooth = 30.0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]

        # Send latest frame to background depth worker
        with depth_lock:
            latest_frame_for_depth = frame
            current_depth = latest_depth_map.copy()

        # Calculate Camera / YOLO FPS
        now = time.time()
        instant_fps = 1.0 / (now - prev_time) if (now - prev_time) > 0 else 30
        prev_time = now
        fps_smooth = 0.9 * fps_smooth + 0.1 * instant_fps

        # Create Colormapped Depth View (Turbo heatmap: Red/Yellow = close, Blue = far)
        depth_color = cv2.applyColorMap(current_depth, cv2.COLORMAP_TURBO)

        # Run ultra-fast YOLO26n detection on current frame (10-15ms)
        results = yolo_model(frame, verbose=False, conf=0.40)

        # Central corridor lines
        c_left, c_right = int(w * 0.30), int(w * 0.70)
        cv2.line(frame, (c_left, 0), (c_left, h), (255, 200, 0), 1)
        cv2.line(frame, (c_right, 0), (c_right, h), (255, 200, 0), 1)

        highest_alert = None
        max_proximity = 0

        # Check for unclassified close obstacles (e.g. hand, wall, closed door)
        # Sample center 30% of depth map
        center_roi = current_depth[int(h*0.3):int(h*0.7), int(w*0.35):int(w*0.65)]
        center_depth_val = np.percentile(center_roi, 85) if center_roi.size > 0 else 0

        if center_depth_val > 215:
            highest_alert = ("Caution: Hand or obstacle right in front of camera", "central_block", (0, 0, 255))
            max_proximity = center_depth_val

        # Evaluate YOLO26n detections with Depth Map values
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0].item())
                label = yolo_model.names[cls_id]
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())

                # Sample true depth inside this object's bounding box
                obj_depth_crop = current_depth[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
                if obj_depth_crop.size == 0:
                    continue
                obj_depth = np.median(obj_depth_crop)

                mid_x = (x1 + x2) / 2.0
                in_corridor = (c_left <= mid_x <= c_right)

                # Determine True Distance Category using Depth Map
                if obj_depth > 210:
                    status = "VERY CLOSE"
                    color = (0, 0, 255)  # Red
                    priority = 2
                    msg = f"Warning, {label} very close"
                elif in_corridor and obj_depth > 160:
                    status = "AHEAD (~2m)"
                    color = (0, 165, 255)  # Orange
                    priority = 1
                    msg = f"{label} ahead"
                else:
                    status = "FAR / SAFE"
                    color = (0, 255, 0)  # Green
                    priority = 0
                    msg = None

                if priority >= 1 and obj_depth > max_proximity and msg:
                    max_proximity = obj_depth
                    highest_alert = (msg, f"{label}_{status}", color)

                # Draw bounding box and depth score
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                tag = f"{label} [{status}]"
                cv2.putText(frame, tag, (x1, max(20, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
                # Draw corresponding box on depth map
                cv2.rectangle(depth_color, (x1, y1), (x2, y2), (255, 255, 255), 1)

        # Trigger audio
        if highest_alert:
            msg, key, _ = highest_alert
            speak_alert(msg, key=key)

        # Overlay Top Status Bars
        cv2.rectangle(frame, (0, 0), (w, 45), (20, 20, 20), -1)
        alert_str = highest_alert[0] if highest_alert else "Path Clear"
        alert_col = highest_alert[2] if highest_alert else (0, 255, 0)
        cv2.putText(frame, f"ALERT: {alert_str.upper()}", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, alert_col, 2)
        cv2.putText(frame, f"CAMERA: {int(fps_smooth)} FPS", (w - 150, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.rectangle(depth_color, (0, 0), (w, 45), (20, 20, 20), -1)
        cv2.putText(depth_color, f"DEPTH REFRESH: {depth_hz:.1f} Hz", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        # Combine side-by-side: [RGB Camera + YOLO26n (30 FPS)] | [Depth Map]
        combined = np.hstack((frame, depth_color))
        cv2.imshow("VX AI - YOLO26n + Depth Anything V2", combined)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            break

    is_running = False
    cap.release()
    cv2.destroyAllWindows()
    speak_alert("VX AI stopped.")
    time.sleep(1)
    speech_queue.put(None)
    print("[Info] Finished.")

if __name__ == "__main__":
    main()
