# VX AI

An intelligent real-time computer vision and depth estimation assistance system integrating YOLO object detection, Depth Anything V2, and Google Gemini Vision for environmental perception and audio feedback.

## Features

- **Object Detection**: Fast real-time detection powered by YOLO models (`yolo26n.onnx`, `yolo11n.pt`, `yolov8n.pt`).
- **Depth Estimation**: Metric depth perception powered by `Depth Anything V2` ONNX runtime (`depth_anything_v2_vits.onnx`).
- **Multimodal Scene Understanding**: Deep scene analysis and visual questioning using Google Gemini Vision API.
- **Audio Feedback**: Non-blocking prioritized TTS voice alerts for proximity warnings and scene descriptions.

## Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Kishan0029/VX_AI.git
   cd VX_AI
   ```

2. **Install dependencies:**
   ```bash
   pip install opencv-python numpy onnxruntime pillow python-dotenv google-genai pywin32 ultralytics
   ```

3. **Configure API Keys:**
   Open `.env` and add your Google Gemini API key:
   ```env
   GEMINI_API_KEY=your_gemini_api_key_here
   ```

## Running

- **Master System:**
  ```cmd
  run_master.bat
  ```
  or:
  ```bash
  python vx_ai_master.py
  ```

- **Live Test:**
  ```cmd
  run_live_test.bat
  ```

- **Depth & YOLO Test:**
  ```cmd
  run_yolo26_depth_test.bat
  ```

- **Gemini Vision Test:**
  ```cmd
  run_gemini_test.bat
  ```
