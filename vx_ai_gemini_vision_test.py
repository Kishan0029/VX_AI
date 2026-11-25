import os
import cv2
import time
import queue
import threading
import pythoncom
import win32com.client
from PIL import Image
from google import genai
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# ==========================================
# 1. API Key Setup
# ==========================================
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    print("=" * 65)
    print("  GEMINI API KEY REQUIRED")
    print("  Get a free key in 30 seconds at: https://aistudio.google.com/app/apikey")
    print("=" * 65)
    api_key = input("Paste your Gemini API Key here: ").strip()
    if api_key:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"\nGEMINI_API_KEY={api_key}\n")
        print(f"[Saved] API key saved to .env for future runs!\n")
    else:
        print("[Error] No API key provided. Exiting.")
        exit(1)

# Initialize Gemini Client
client = genai.Client(api_key=api_key)

# ==========================================
# 2. Non-Blocking Voice Worker
# ==========================================
speech_queue = queue.Queue()

def tts_worker():
    pythoncom.CoInitialize()
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    speaker.Rate = 1  # Slightly brisk pace for assistive feedback
    while True:
        text = speech_queue.get()
        if text is None:
            break
        try:
            print(f"\n[VX AI Earphones]: \"{text}\"\n", flush=True)
            speaker.Speak(text)
        except Exception as e:
            print(f"[TTS Error]: {e}", flush=True)
        speech_queue.task_done()

voice_thread = threading.Thread(target=tts_worker, daemon=True)
voice_thread.start()

def speak(text):
    while not speech_queue.empty():
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except queue.Empty:
            break
    speech_queue.put(text)

# ==========================================
# 3. Gemini Vision Analyzer
# ==========================================
PROMPT = (
    "You are VX AI, an assistive eyes system for a visually impaired user wearing a chest-mounted camera with earphones. "
    "Analyze this image carefully:\n"
    "1. Identify immediate physical obstacles, terrain changes (steps, elevation, stairs), and hazards.\n"
    "2. If the user's hand or arm is held up near the lens, explicitly identify it: e.g. 'Your hand is in front of the camera.'\n"
    "3. State direction (left, right, center, or clock directions) and estimated distance in meters or arm's length.\n"
    "4. Be concise and natural (maximum 1 to 2 sentences), suitable to be spoken through earphones.\n"
    "5. If path is clear, say: 'Path is clear ahead.'"
)

is_analyzing = False
last_description = "Press SPACE to analyze, or 'A' for Auto Mode"

def analyze_frame_async(frame_bgr):
    global is_analyzing, last_description
    if is_analyzing:
        return
    is_analyzing = True
    
    def worker():
        global is_analyzing, last_description
        try:
            # Convert OpenCV BGR to RGB PIL Image
            rgb_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb_frame)

            # Call Gemini 2.0 Flash
            print("[Gemini] Analyzing scene...", flush=True)
            t0 = time.time()
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[pil_img, PROMPT]
                )
            except Exception:
                response = client.models.generate_content(
                    model="gemini-flash-latest",
                    contents=[pil_img, PROMPT]
                )
            elapsed = time.time() - t0
            desc = response.text.strip().replace("\n", " ")
            print(f"[Gemini ({elapsed:.2f}s)]: {desc}", flush=True)
            last_description = desc
            speak(desc)
        except Exception as e:
            err = f"Analysis error: {str(e)[:100]}"
            print(err, flush=True)
            last_description = err
        finally:
            is_analyzing = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()

# ==========================================
# 4. Main Webcam Loop
# ==========================================
def main():
    print("=" * 65, flush=True)
    print("  VX AI - Gemini Vision Assistive Test", flush=True)
    print("  Controls:", flush=True)
    print("    [SPACEBAR] : Capture & Analyze current view", flush=True)
    print("    [A]        : Toggle Auto Mode (analyzes every 4 seconds)", flush=True)
    print("    [Q / ESC]  : Quit", flush=True)
    print("=" * 65, flush=True)

    speak("VX AI cloud vision test ready. Press spacebar or A for auto.")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[Error] Could not open webcam.")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    auto_mode = False
    last_auto_time = 0
    AUTO_INTERVAL = 4.0  # seconds

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape

        # Auto Mode Trigger
        now = time.time()
        if auto_mode and (now - last_auto_time > AUTO_INTERVAL) and not is_analyzing:
            last_auto_time = now
            analyze_frame_async(frame.copy())

        # Draw UI Overlay
        cv2.rectangle(frame, (0, 0), (w, 65), (20, 20, 20), -1)
        status_mode = "AUTO MODE [ON]" if auto_mode else "MANUAL [SPACE to capture]"
        status_color = (0, 255, 100) if auto_mode else (0, 200, 255)
        if is_analyzing:
            status_mode = "ANALYZING SCENE..."
            status_color = (0, 140, 255)

        cv2.putText(frame, status_mode, (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, status_color, 2)
        cv2.putText(frame, "SPACE: Trigger | A: Toggle Auto | Q: Quit", (15, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)

        # Draw latest AI message at bottom
        cv2.rectangle(frame, (0, h - 55), (w, h), (20, 20, 20), -1)
        display_text = (last_description[:80] + "...") if len(last_description) > 80 else last_description
        cv2.putText(frame, display_text, (15, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        cv2.imshow("VX AI - Gemini Cloud Vision", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord(' ') and not is_analyzing:
            analyze_frame_async(frame.copy())
        elif key == ord('a') or key == ord('A'):
            auto_mode = not auto_mode
            state = "enabled" if auto_mode else "disabled"
            print(f"[Mode] Auto mode {state}.", flush=True)
            speak(f"Auto mode {state}")
        elif key == ord('q') or key == 27:
            break

    cap.release()
    cv2.destroyAllWindows()
    speak("VX AI stopped.")
    time.sleep(1)
    speech_queue.put(None)
    print("\n[Info] Test finished.")

if __name__ == "__main__":
    main()
