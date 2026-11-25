import cv2
import time
import queue
import threading
import pythoncom
import win32com.client
from ultralytics import YOLO

# ==========================================
# 1. Asynchronous Non-Blocking Voice Worker
# ==========================================
speech_queue = queue.Queue()
last_spoken_time = {}
SPEECH_COOLDOWN = 3.5  # Seconds before repeating the same alert

def tts_worker():
    """Background worker for Windows SAPI TTS to avoid freezing video frames."""
    pythoncom.CoInitialize()
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1  # Slightly faster for real-time assistive guidance
    
    while True:
        text = speech_queue.get()
        if text is None:
            break
        try:
            print(f"[Voice Output]: \"{text}\"", flush=True)
            speaker.Speak(text)
        except Exception as e:
            print(f"[TTS Error]: {e}", flush=True)
        speech_queue.task_done()

# Start background speech thread
voice_thread = threading.Thread(target=tts_worker, daemon=True)
voice_thread.start()

def speak_alert(message, key=None):
    """Enqueue speech with a cooldown to avoid speech spamming."""
    now = time.time()
    lookup_key = key if key else message
    if lookup_key in last_spoken_time:
        if now - last_spoken_time[lookup_key] < SPEECH_COOLDOWN:
            return  # Skip, cooldown active
    
    last_spoken_time[lookup_key] = now
    # If queue is getting backed up, purge older low-priority items
    while speech_queue.qsize() > 1:
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except queue.Empty:
            break
    speech_queue.put(message)

# ==========================================
# 2. Main Live Vision System
# ==========================================
def main():
    print("=" * 60, flush=True)
    print("  VX AI - Assistive Vision & Navigation Live Prototype", flush=True)
    print("  Connected to your webcam & earphones/speakers", flush=True)
    print("  Press 'q' or 'ESC' on the camera window to quit.", flush=True)
    print("=" * 60, flush=True)

    # Initial greeting through earphones
    speak_alert("VX AI assistant started. Ready.")

    print("[Info] Loading YOLOv8 model...", flush=True)
    model = YOLO("yolov8n.pt")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[Error] Could not open webcam (device 0). Please check camera connections/permissions.", flush=True)
        return

    # Set camera resolution
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # Central walking corridor boundaries (normalized)
    CORRIDOR_LEFT = 0.30
    CORRIDOR_RIGHT = 0.70

    prev_time = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[Warning] Failed to grab frame from webcam.", flush=True)
            break

        # Flip horizontally for natural mirror feel (laptop webcam)
        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape

        # Calculate FPS
        curr_time = time.time()
        fps = 1.0 / (curr_time - prev_time) if (curr_time - prev_time) > 0 else 30
        prev_time = curr_time

        # Draw corridor guide lines (Navigation Corridor)
        x_left = int(w * CORRIDOR_LEFT)
        x_right = int(w * CORRIDOR_RIGHT)
        cv2.line(frame, (x_left, 0), (x_left, h), (255, 200, 0), 1)
        cv2.line(frame, (x_right, 0), (x_right, h), (255, 200, 0), 1)
        cv2.putText(frame, "WALKING CORRIDOR", (x_left + 10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 200, 0), 1)

        # Run inference (stream=True for faster generator processing)
        results = model(frame, verbose=False, conf=0.45)

        highest_alert = None
        highest_priority = -1  # 2: Danger/Warning, 1: Caution ahead, 0: Info

        for r in results:
            boxes = r.boxes
            for box in boxes:
                cls_id = int(box.cls[0].item())
                label = model.names[cls_id]
                conf = float(box.conf[0].item())

                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                box_w = x2 - x1
                box_h = y2 - y1

                # Normalized metrics
                mid_x = (x1 + x2) / (2.0 * w)
                rel_h = box_h / float(h)
                rel_area = (box_w * box_h) / float(w * h)

                in_corridor = (CORRIDOR_LEFT <= mid_x <= CORRIDOR_RIGHT)

                # Determine Proximity & Danger Level
                # Level 2 (DANGER / VERY CLOSE): Object takes up large portion of frame
                if rel_h > 0.45 or rel_area > 0.20:
                    status = "WARNING"
                    color = (0, 0, 255)  # Red
                    priority = 2
                    msg = f"Warning, {label} very close"
                # Level 1 (CAUTION / IN WALKING PATH): Ahead in walking corridor
                elif in_corridor and (rel_h > 0.22 or rel_area > 0.08):
                    status = "AHEAD"
                    color = (0, 165, 255)  # Orange
                    priority = 1
                    msg = f"{label} ahead in your path"
                # Level 0 (SAFE / OFF TO THE SIDE):
                else:
                    status = "SAFE"
                    color = (0, 255, 0)  # Green
                    priority = 0
                    msg = None

                # Keep track of highest priority threat in the frame
                if priority > highest_priority and msg:
                    highest_priority = priority
                    highest_alert = (msg, label, status)

                # Draw bounding box & label
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                tag = f"{label} {int(conf*100)}% [{status}]"
                cv2.putText(frame, tag, (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Trigger speech for the highest priority detection
        if highest_alert:
            msg, label, status = highest_alert
            speak_alert(msg, key=f"{label}_{status}")

        # Top Overlay Panel
        cv2.rectangle(frame, (0, 0), (w, 50), (30, 30, 30), -1)
        if highest_alert:
            status_text = f"ALERT: {highest_alert[0].upper()}"
            status_color = (0, 0, 255) if highest_priority == 2 else (0, 165, 255)
        else:
            status_text = "PATH CLEAR"
            status_color = (0, 255, 0)

        cv2.putText(frame, status_text, (15, 33), cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
        cv2.putText(frame, f"FPS: {int(fps)}", (w - 100, 33), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

        cv2.imshow("VX AI - Live Vision Prototype", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:  # 'q' or ESC
            break

    # Clean up
    cap.release()
    cv2.destroyAllWindows()
    speak_alert("VX AI stopped.")
    time.sleep(1)
    speech_queue.put(None)
    print("\n[Info] Live test finished cleanly.", flush=True)

if __name__ == "__main__":
    main()
