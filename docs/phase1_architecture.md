# Dump2Done Phase 1 Architecture Blueprint

本文是 Dump2Done 第一階段交付物：平台效能評估、系統架構設計、NVIDIA-ready 規劃與 MVP 實作藍圖。第一階段刻意不做完整商業化平台，重點是把長影片、可恢復 pipeline、後端抽象、GPU/NVIDIA 遷移能力先打好。

## 1. 產品定位

### 產品角度

Dump2Done 不是單一短影音剪輯器，而是本地端 AI 影片自動後製平台。核心價值是：使用者丟入長影片，系統理解內容、找精華、重構成不同平台格式，並自動完成字幕、裁切、節奏、音訊與輸出。

第一批目標使用者應是創作者、課程製作者、Podcast/訪談剪輯者、行銷團隊與需要保留素材隱私的工作室。產品差異化在於本地端、長影片優先、可 debug、可 resume、可替換模型與可移植到 NVIDIA GPU server。

### 系統角度

Dump2Done 應被設計成 job-based media AI pipeline，而不是單一 Python script。每支影片是一個 job，每個階段輸出中間 artifact，後續階段只讀 artifact，不重新推理已完成結果。核心系統能力包括：

- chunk-based processing：長影片分段 ASR、語意分析、CV 偵測。
- artifact-driven resume：任何階段失敗後可從最近完成 artifact 繼續。
- backend-agnostic inference：ASR、LLM、Vision、Render 都有介面層。
- NVIDIA-ready worker split：未來 ASR / LLM / CV / Render 可拆成獨立 GPU worker。

### 工程角度

Phase 1 先建立工程邊界與效能探針：`check_env.py`、`config.yaml`、資料格式、CLI 規格、模組界面。Phase 2 才實作可跑通單支影片的 local MVP。現在不應急著做 Web UI、多使用者、多 GPU scheduler、完整配樂/配音，因為這些會在 pipeline artifact 邊界穩定前製造過早耦合。

## 2. 第一階段目標

Phase 1 要完成：

1. 評估 OS、Python、FFmpeg、CPU/RAM/Disk、NVIDIA GPU/CUDA/NVENC、ASR、LLM、Vision 能力。
2. 將目前平台分成 Level A/B/C/D，並額外評估 NVIDIA-ready Level N。
3. 設計 Windows/Linux local-first，未來可 containerize 的架構。
4. 定義 MVP 範圍與不做項目。
5. 規劃 Phase 2、Phase 3、Phase 4 roadmap。
6. 設計專案目錄、模組邊界、CLI、config、artifact 格式。
7. 提供 `check_env.py`。
8. 指出必須抽象化的模組：ASR backend、LLM backend、Vision backend、Render backend、Storage/Job state。

## 3. 平台效能檢查方案

`check_env.py` 應以標準函式庫為主，選擇性偵測 `torch`、`psutil`、`faster_whisper`、`ctranslate2`、`cv2`、`mediapipe`。不要要求使用者先安裝完整依賴才可檢查。

檢查項目：

- OS/Python/tools：OS、Python、pip、venv、FFmpeg、FFprobe、Git。
- CPU/RAM/Disk：CPU 型號、threads、RAM、可用 RAM、workspace/temp disk free。
- GPU/CUDA：`nvidia-smi`、GPU 數量、VRAM、Driver、CUDA、`nvcc`、PyTorch CUDA、FP16 推定。
- FFmpeg codecs：`h264_nvenc`、`hevc_nvenc`、`av1_nvenc`、`h264_cuvid`、`hevc_cuvid`、`libx264`、`libx265`。
- ASR：`faster_whisper`、`ctranslate2`、建議 model/compute type。
- LLM：Ollama binary、Ollama API、local model list、未來 vLLM/llama.cpp 條件。
- Vision：OpenCV、MediaPipe、OpenCV CUDA、長影片偵測策略。

已提供根目錄的 [check_env.py](../check_env.py)。

## 4. 硬體分級規則

### Level A：高階本地工作站

條件：

- NVIDIA GPU VRAM >= 16GB
- RAM >= 32GB
- CUDA 正常
- NVENC 正常
- FFmpeg GPU encode 可用

建議：

- ASR：`large-v3` 或 `distil-large-v3`
- compute type：`float16`
- LLM：7B/8B instruct model
- Smart Crop：5-10 fps 偵測
- output：1080x1920, 30fps
- encoder：`h264_nvenc` 或 `hevc_nvenc`
- 長影片仍應分段處理，避免一次塞進 RAM/context。

### Level B：中階本地工作站

條件：

- NVIDIA GPU VRAM 8GB-12GB
- RAM 16GB-32GB
- CUDA 可用
- NVENC 可用或部分可用

建議：

- ASR：`distil-large-v3` 或 `medium`
- compute type：`float16` 或 `int8_float16`
- LLM：量化 7B instruct model
- Smart Crop：3-5 fps
- output：720x1280 或 1080x1920
- encoder：優先 NVENC，失敗 fallback `libx264`
- 30 分鐘影片必須 segment pipeline。

### Level C：入門 GPU 平台

條件：

- VRAM <= 6GB
- RAM <= 16GB
- CUDA 可用但資源有限

建議：

- ASR：`small`、`medium` 或 `distil-large-v3 int8`
- compute type：`int8` 或 `int8_float16`
- LLM：小型量化模型或外部 LLM fallback
- Smart Crop：1-3 fps
- output：720x1280
- 先用 1-3 分鐘測試片段驗證。

### Level D：CPU-only 或無穩定 CUDA

條件：

- 無 NVIDIA GPU，或 CUDA/NVENC 不可用

建議：

- ASR：CPU `int8`
- LLM：小型 CPU 量化模型或外部 API
- Smart Crop：極低頻偵測 + 插值
- render：`libx264`
- 僅適合功能驗證，不適合大量長影片。

## 5. NVIDIA-ready 分級規則

Level N 不只看單機 GPU，而看是否可成為 future deployment node。

- N0：無穩定 CUDA/NVENC 或無 container runtime。只能做 local MVP。
- N1：單 GPU + CUDA + NVENC，可 Docker 化，可跑單 worker。
- N2：多 GPU 或高 VRAM + Docker GPU runtime，可拆 ASR/CV/Render worker。
- N3：具備 job queue、object storage、model registry、monitoring，可導入 Triton/TensorRT 與 batch processing。

未來移植 NVIDIA 系統時，架構要從 local CLI 演進成 API server + queue + GPU workers + artifact storage。ASR、Vision model、部分 LLM inference 可逐步導入 Triton/TensorRT；render 保持 FFmpeg/NVENC 為主。

## 6. 系統總架構

### 文字版 architecture diagram

```text
User / CLI / Future Web UI
        |
        v
Job Manager + Config Loader
        |
        v
Artifact Store: output/jobs/{job_id}/...
        |
        +--> Input Ingestion
        +--> Metadata Analysis (ffprobe)
        +--> Audio Extraction (ffmpeg)
        +--> ASR Backend (faster-whisper now, WhisperX later)
        +--> Transcript Segmentation
        +--> LLM Backend (Ollama now, vLLM/llama.cpp/API/Triton later)
        +--> Clip Candidate Selection
        +--> Clip Validation
        +--> Vision Analysis (MediaPipe/OpenCV now, YOLO/TensorRT later)
        +--> Smart Reframing
        +--> Subtitle Planning + ASS Generation
        +--> Music Plan (future)
        +--> Voiceover Plan (future)
        +--> Render Plan
        +--> Render Backend (FFmpeg CPU/NVENC)
        +--> Quality Scoring
        +--> Export Packaging + Debug Report
```

### Pipeline dataflow

```text
input.mp4
  -> job_manifest.json
  -> video_info.json
  -> audio/audio.wav + audio_info.json
  -> transcripts/transcript.json + words.json
  -> transcripts/segments.json
  -> llm/llm_input.chunk_*.json
  -> llm/clip_candidates.json
  -> clips/validated_clips.json
  -> crop_tracks/crop_track.{clip_id}.json
  -> subtitles/subtitle_plan.{clip_id}.json
  -> subtitles/{clip_id}.ass
  -> render/render_plan.{clip_id}.json
  -> renders/{clip_id}.mp4
  -> reports/render_report.json + quality_report.json
```

### 模組 input/output 與責任

| Module | Input | Output | Responsibility | Phase 2 Sync/Worker |
|---|---|---|---|---|
| Input ingestion | input path, config | `job_manifest.json` | 建立 job、複製或引用 input | sync now |
| Metadata analysis | input video | `video_info.json` | ffprobe metadata | sync |
| Audio extraction | video | `audio.wav`, `audio_info.json` | 抽 16k mono WAV | sync, worker later |
| ASR | audio chunks | `transcript.json`, `words.json` | word-level ASR | worker later |
| Segmentation | words/transcript | `segments.json` | 3-5 分鐘 chunk + sentence segments | sync |
| LLM analysis | segments | `clip_candidates.json` | 找 highlight | worker/model server later |
| Validation | candidates, words | `validated_clips.json` | duration、overlap、boundary 修正 | sync |
| Vision analysis | video + clips | detections cache | face/object/speaker cues | worker later |
| Smart crop | detections + clips | `crop_track.json` | 平滑 crop path | sync/worker |
| Subtitle | words + clips | `subtitle_plan.json`, `.ass` | ASS subtitle | sync |
| Music | clip metadata | `music_plan.json` | future music selection | future worker |
| Voiceover | clip summary | `voiceover_plan.json` | future TTS plan | future worker |
| Render | render plan | mp4, `render_report.json` | FFmpeg/NVENC export | worker later |
| Quality scoring | render + artifacts | `quality_report.json` | basic QA | sync/worker |
| Report | all artifacts | debug report | audit/debug | sync |

Containerize first: ASR worker, LLM server/worker, Vision worker, Render worker. Keep CLI/job manager local until Phase 4.

## 7. 架構設計原則

### Modular-first

每個 domain module 只做一件事，不能讓 `main.py` 包含所有邏輯。`main.py` 只負責 CLI routing、config loading、job orchestration。

### Backend-agnostic

需要介面層：

- `ASRBackend.transcribe(audio, options) -> TranscriptArtifact`
- `LLMBackend.select_clips(llm_input, schema) -> ClipCandidates`
- `VisionBackend.detect(video, time_range, fps) -> DetectionTrack`
- `RenderBackend.render(render_plan) -> RenderReport`

現在可先做 simple factory，不急著做複雜 plugin system。

### Config-driven

所有 model、device、compute type、輸出尺寸、字幕樣式、encoder、clip duration、cache/debug 都從 YAML 來。CLI override config，job artifact 記錄實際使用值。

### Intermediate artifacts

每階段輸出 JSON/WAV/ASS/MP4。Artifact 要有 `status`、`inputs`、`outputs`、`errors`、`created_at`、`config_hash`，以支援 resume/rerun。

### Long-video first

避免：

- 整支影片載入 RAM
- 所有 frame 存 memory
- 完整逐字稿一次丟 LLM
- 每幀偵測臉/物件
- 單一 process 管所有重任務

採用：

- 3-5 分鐘 chunks
- timeline index
- artifact cache
- resumable job state
- segment-level LLM analysis + global ranking
- low-fps detection + interpolation

## 8. MVP 範圍

### Phase 1：Architecture + Environment Probe

已交付：

- `check_env.py`
- `configs/default.yaml`
- 本架構文件
- 分級規則、artifact schema、prompt/schema、CLI、roadmap

### Phase 2：Local MVP

必做：

1. 單支影片輸入。
2. FFprobe 影片資訊分析。
3. FFmpeg 抽音訊。
4. faster-whisper word-level transcription。
5. 本地 LLM 找 3-5 個候選片段。
6. clip validation。
7. 切出短影片。
8. 16:9 -> 9:16 smart crop。
9. 產生 ASS 字幕。
10. FFmpeg 輸出 MP4。
11. debug report。
12. CLI 分步執行。

暫不做：

- 完整配樂、完整配音：需要版權、ducking、TTS timing，先只留 artifact。
- Web UI、多使用者、queue、多 GPU：job/artifact 邊界未穩定前不值得。
- 自動上傳與商業模板：不是 MVP 風險核心。

### Phase 3：Creator Automation

加入自動配樂、旁白、靜音移除、punch-in zoom、關鍵字字幕高亮、自動標題、hashtags、多輸出格式、批次處理、基礎 Web UI。

### Phase 4：NVIDIA-ready Platform

加入 Docker、GPU worker、job queue、API server、multi-GPU scheduling、model server abstraction、Triton/TensorRT backend、centralized storage、monitoring、profiling、batch render farm。

## 9. 專案目錄結構

```text
dump2done/
  README.md
  requirements.txt
  pyproject.toml
  config.yaml
  check_env.py
  main.py
  docker/
    Dockerfile
    docker-compose.yml
    nvidia-runtime-notes.md
  configs/
    default.yaml
    level_a.yaml
    level_b.yaml
    level_c.yaml
    nvidia_server.yaml
  docs/
    phase1_architecture.md
  src/
    dump2done/
      core/
        job.py
        artifacts.py
        timeline.py
        errors.py
      config/
        loader.py
        schema.py
      media/
        ffprobe.py
        audio.py
        video_io.py
      asr/
        base.py
        faster_whisper_backend.py
      llm/
        base.py
        ollama_backend.py
        prompts.py
        schemas.py
      vision/
        base.py
        mediapipe_backend.py
        opencv_backend.py
      crop/
        planner.py
        smoothing.py
      subtitles/
        planner.py
        ass_writer.py
      music/
        planner.py
      voiceover/
        planner.py
      render/
        base.py
        ffmpeg_backend.py
        command_builder.py
      pipeline/
        runner.py
        stages.py
      reports/
        debug_report.py
        quality.py
      utils/
        subprocess.py
        paths.py
        json_io.py
  output/
    jobs/
      job_id/
        input/
        audio/
        transcripts/
        llm/
        clips/
        crop_tracks/
        subtitles/
        renders/
        reports/
        cache/
  tests/
```

用途：

- `core/`：job state、artifact metadata、timeline、共用錯誤型別。
- `config/`：YAML loading、profile merge、CLI override。
- `media/`：FFmpeg/FFprobe 與 media I/O。
- `asr/llm/vision/render/`：backend 抽象和具體實作。
- `crop/subtitles/music/voiceover/`：後製計畫產生器。
- `pipeline/`：stage orchestration 和 resume。
- `reports/`：debug、quality、render report。
- `output/jobs/`：所有 job artifact，路徑都相對 job directory。

## 10. 中間 JSON 格式

通用規則：

- 所有時間用秒數 float。
- 所有 path 使用相對 job directory。
- 每個 artifact 有 `schema_version`、`stage`、`status`、`created_at`、`inputs`、`outputs`、`errors`。
- `status` 建議：`pending | running | completed | failed | skipped`.
- 支援 resume：artifact 可被檢查完整性和 config hash。

### `job_manifest.json`

```json
{
  "schema_version": "1.0",
  "job_id": "demo001",
  "status": "running",
  "created_at": "2026-07-06T10:00:00Z",
  "input": {
    "source_path": "input/input.mp4",
    "original_filename": "talk.mp4",
    "content_hash": "sha256:..."
  },
  "config": {
    "profile": "default",
    "config_path": "configs/default.yaml",
    "effective_config_path": "reports/effective_config.yaml"
  },
  "stages": {
    "analyze": "completed",
    "transcribe": "pending",
    "select_clips": "pending",
    "crop": "pending",
    "subtitle": "pending",
    "render": "pending"
  },
  "errors": []
}
```

### `video_info.json`

```json
{
  "schema_version": "1.0",
  "stage": "analyze",
  "status": "completed",
  "duration": 1832.45,
  "width": 1920,
  "height": 1080,
  "fps": 29.97,
  "video_codec": "h264",
  "audio_codec": "aac",
  "streams": [{"index": 0, "type": "video"}, {"index": 1, "type": "audio"}],
  "outputs": {"video_info": "reports/video_info.json"},
  "errors": []
}
```

### `audio_info.json`

```json
{
  "schema_version": "1.0",
  "stage": "extract_audio",
  "status": "completed",
  "audio_path": "audio/audio.wav",
  "duration": 1832.45,
  "sample_rate": 16000,
  "channels": 1,
  "format": "wav",
  "errors": []
}
```

### `transcript.json`

```json
{
  "schema_version": "1.0",
  "stage": "transcribe",
  "status": "completed",
  "language": "zh",
  "backend": "faster_whisper",
  "model": "distil-large-v3",
  "segments": [
    {"id": 0, "start": 0.0, "end": 5.24, "text": "今天我們來談 AI 影片後製。"}
  ],
  "errors": []
}
```

### `words.json`

```json
{
  "schema_version": "1.0",
  "stage": "transcribe_words",
  "status": "completed",
  "words": [
    {"start": 0.12, "end": 0.44, "word": "今天", "confidence": 0.92},
    {"start": 0.45, "end": 0.76, "word": "我們", "confidence": 0.91}
  ],
  "errors": []
}
```

### `segments.json`

```json
{
  "schema_version": "1.0",
  "stage": "segment_transcript",
  "status": "completed",
  "chunk_duration": 300.0,
  "segments": [
    {
      "segment_id": "seg_0001",
      "start": 0.0,
      "end": 300.0,
      "text": "..."
    }
  ],
  "errors": []
}
```

### `llm_input.json`

```json
{
  "schema_version": "1.0",
  "stage": "prepare_llm_input",
  "status": "completed",
  "chunk_id": "seg_0001",
  "timeline": [
    {"start": 12.4, "end": 18.2, "text": "這裡是一個完整觀點。"}
  ],
  "constraints": {
    "min_duration": 20.0,
    "max_duration": 90.0,
    "max_candidates": 5
  },
  "errors": []
}
```

### `clip_candidates.json`

```json
{
  "schema_version": "1.0",
  "stage": "select_clips",
  "status": "completed",
  "clips": [
    {
      "clip_id": "clip_001",
      "title": "AI 後製真正省下的是決策時間",
      "start_time": 124.5,
      "end_time": 178.0,
      "hook_text": "很多人以為 AI 剪片只是把影片切短。",
      "summary": "說明 AI 後製從理解內容到重構節奏的價值。",
      "content_type": "insight",
      "virality_score": 86,
      "clarity_score": 90,
      "standalone_score": 88,
      "retention_score": 82,
      "reason": "前三秒有反差，且觀點完整。",
      "keywords": ["AI 後製", "決策時間"],
      "recommended_format": "9:16"
    }
  ],
  "errors": []
}
```

### `validated_clips.json`

```json
{
  "schema_version": "1.0",
  "stage": "validate_clips",
  "status": "completed",
  "clips": [
    {
      "clip_id": "clip_001",
      "start_time": 123.8,
      "end_time": 179.2,
      "duration": 55.4,
      "source_candidate_id": "clip_001",
      "validation": {
        "duration_ok": true,
        "overlap_ok": true,
        "has_words": true,
        "adjusted_to_sentence_boundary": true
      }
    }
  ],
  "errors": []
}
```

### `crop_track.json`

```json
{
  "schema_version": "1.0",
  "stage": "smart_crop",
  "status": "completed",
  "clip_id": "clip_001",
  "source_resolution": [1920, 1080],
  "target_resolution": [1080, 1920],
  "target_format": "9:16",
  "frames": [
    {"time": 123.8, "x": 656, "y": 0, "w": 608, "h": 1080, "confidence": 0.88, "source": "face"}
  ],
  "smoothing": {"method": "ema", "alpha": 0.25, "dead_zone_ratio": 0.08},
  "errors": []
}
```

### `subtitle_plan.json`

```json
{
  "schema_version": "1.0",
  "stage": "subtitle",
  "status": "completed",
  "clip_id": "clip_001",
  "language": "zh-Hant",
  "style": {
    "format": "ass",
    "font_family": "Noto Sans CJK TC",
    "font_size": 76,
    "outline": 3,
    "shadow": 1,
    "alignment": "bottom_center"
  },
  "captions": [
    {"start": 124.0, "end": 126.2, "text": "很多人以為 AI 剪片", "highlight_words": ["AI"]}
  ],
  "output_ass": "subtitles/clip_001.ass",
  "errors": []
}
```

### `music_plan.json`

```json
{
  "schema_version": "1.0",
  "stage": "music",
  "status": "skipped",
  "clip_id": "clip_001",
  "mood": "energetic",
  "track_path": null,
  "ducking": {"enabled": true, "target_db": -12},
  "fade_in": 0.5,
  "fade_out": 1.0,
  "errors": []
}
```

### `voiceover_plan.json`

```json
{
  "schema_version": "1.0",
  "stage": "voiceover",
  "status": "skipped",
  "clip_id": "clip_001",
  "script": "",
  "language": "zh-Hant",
  "tts_backend": "none",
  "voice": "default",
  "timing": [],
  "errors": []
}
```

### `render_plan.json`

```json
{
  "schema_version": "1.0",
  "stage": "render_plan",
  "status": "completed",
  "clip_id": "clip_001",
  "input_video": "input/input.mp4",
  "start_time": 123.8,
  "end_time": 179.2,
  "target_resolution": [1080, 1920],
  "crop_track": "crop_tracks/clip_001.json",
  "subtitle_ass": "subtitles/clip_001.ass",
  "encoder_preference": ["h264_nvenc", "libx264"],
  "output_path": "renders/clip_001.mp4",
  "errors": []
}
```

### `render_report.json`

```json
{
  "schema_version": "1.0",
  "stage": "render",
  "status": "completed",
  "clip_id": "clip_001",
  "output_path": "renders/clip_001.mp4",
  "encoder_used": "h264_nvenc",
  "ffmpeg_command": ["ffmpeg", "..."],
  "duration_sec": 55.4,
  "elapsed_sec": 18.6,
  "file_size_mb": 52.1,
  "errors": []
}
```

### `quality_report.json`

```json
{
  "schema_version": "1.0",
  "stage": "quality",
  "status": "completed",
  "clip_id": "clip_001",
  "checks": {
    "file_exists": true,
    "duration_match": true,
    "has_audio": true,
    "has_video": true,
    "subtitle_burn_in_expected": true
  },
  "warnings": [],
  "errors": []
}
```

## 11. LLM 語意切片設計

### System Prompt

```text
You are Dump2Done's local video story editor. Your task is to select short, standalone highlight clips from timestamped transcript chunks.

Rules:
- Output strict JSON only.
- Do not output markdown.
- Do not invent facts or wording not supported by the transcript.
- Each clip must be understandable without watching the full source video.
- Prefer clips with a strong first 3 seconds, clear idea, emotion, conflict, tutorial value, reversal, or story arc.
- Clip duration must be between the provided min_duration and max_duration.
- start_time must be less than end_time.
- Scores must be integers from 0 to 100.
- Do not recommend duplicate ideas.
- Clip overlap should not exceed 30%.
- If there are no suitable clips, output {"clips":[]}.
```

### User Prompt

```text
Analyze this transcript chunk and select up to {max_candidates} highlight clips.

Constraints:
- min_duration: {min_duration} seconds
- max_duration: {max_duration} seconds
- target platforms: YouTube Shorts, TikTok, Instagram Reels, YouTube highlights
- recommended formats: 9:16, 1:1, 16:9

Transcript timeline:
{timeline_json}

Return JSON matching the provided schema exactly.
```

### JSON Schema

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["clips"],
  "properties": {
    "clips": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "title",
          "start_time",
          "end_time",
          "hook_text",
          "summary",
          "content_type",
          "virality_score",
          "clarity_score",
          "standalone_score",
          "retention_score",
          "reason",
          "keywords",
          "recommended_format"
        ],
        "properties": {
          "title": {"type": "string"},
          "start_time": {"type": "number"},
          "end_time": {"type": "number"},
          "hook_text": {"type": "string"},
          "summary": {"type": "string"},
          "content_type": {
            "type": "string",
            "enum": ["story", "insight", "controversy", "tutorial", "reaction", "news", "other"]
          },
          "virality_score": {"type": "integer", "minimum": 0, "maximum": 100},
          "clarity_score": {"type": "integer", "minimum": 0, "maximum": 100},
          "standalone_score": {"type": "integer", "minimum": 0, "maximum": 100},
          "retention_score": {"type": "integer", "minimum": 0, "maximum": 100},
          "reason": {"type": "string"},
          "keywords": {"type": "array", "items": {"type": "string"}},
          "recommended_format": {"type": "string", "enum": ["9:16", "1:1", "16:9"]}
        }
      }
    }
  }
}
```

### Long-video strategy

30 分鐘影片不一次丟完整 transcript。先將 transcript 分成 3-5 分鐘 chunks，每個 chunk 產出候選，validation 後做 global ranking。global ranking 可用加權分數：

```text
score = 0.35 * virality + 0.25 * standalone + 0.20 * retention + 0.20 * clarity
```

再做 overlap suppression，輸出 top N。

## 12. Smart Crop 策略

第一版目標是穩，不是追求複雜電影感。

流程：

1. 對 clip time range 以低頻偵測取樣。
2. 使用 MediaPipe Face Detection 找臉框。
3. OpenCV fallback：Haar cascade 或簡單 motion/saliency heuristic。
4. 多人時選擇策略：最大臉、最接近畫面中心、連續出現時間最長者。若有 speaker diarization/active speaker detection，未來改選說話者。
5. 偵測不到臉：center crop。
6. 產生 sparse crop anchors。
7. 插值到 render 需要的時間點。
8. EMA smoothing，必要時 Kalman filter。
9. dead zone 避免小幅抖動。
10. max movement speed 限制 crop window 移動速度。
11. clamp crop box，不得超出 source frame。

建議 detection fps：

- Level A：5-10 fps。
- Level B：3-5 fps。
- Level C：1-3 fps。
- Level D：0.5-1 fps 或只 center crop。

輸出 `crop_track.json`，每個 time point 包含 `x/y/w/h/confidence/source`。

## 13. 字幕策略

使用 ASS 而不是只用 SRT，原因是 ASS 支援樣式、字體、描邊、陰影、位置、顏色、關鍵字 highlight，較適合短影音 burn-in。SRT 保留給平台 sidecar subtitle，但 MVP render 應以 ASS burn-in 為主。

word-level timestamps 轉 caption chunks：

- 中文：依標點、語意停頓與 8-14 字切行。
- 英文：每 3-6 words 一行，避免超過 2 行。
- 中英混合：以 word timestamp 為主，CJK 字元按字數估算寬度，英文按 word grouping。
- 每個 caption 0.8-3.0 秒為佳，太短合併，太長拆分。
- 關鍵字 highlight：在 `subtitle_plan.json` 標注 `highlight_words`，ASS writer 轉成 override tags。
- 字型 fallback：Windows 可先用 Microsoft JhengHei / Arial；Linux 建議安裝 Noto Sans CJK。config 允許指定字型路徑，不要 hard-code。

9:16 字幕位置建議：底部安全區上方，避開平台 UI；必要時對 TikTok/Reels profile 加不同 `MarginV`。

## 14. 配樂未來架構

Phase 1/2 只設計 `music_plan.json`，不做完整功能。

Music selection policy：

- 根據 clip content type、情緒、節奏、長度選 track。
- 本地 music library metadata 包含 mood、bpm、license、energy、loopable。
- beat detection 可用 librosa 或 aubio，但 Phase 2 不引入。

Audio ducking：

- 以 voice track RMS/voice activity 為 sidechain reference。
- 人聲區間降低音樂 -8 到 -16 dB。
- 開頭/結尾 fade in/out。

FFmpeg mixing：

- `amix` 混合 voice/music。
- `sidechaincompress` 或分段 volume automation 做 ducking。
- 先輸出 mixed audio artifact，再 render。

## 15. 配音未來架構

Voiceover module 應有 TTS backend abstraction：

- `TTSBackend.synthesize(script, voice, language) -> audio_path + word timings`
- future backends：local TTS、OpenAI-compatible TTS、NVIDIA Riva、其他服務。

Voiceover script prompt：

```text
Create a concise voiceover script for this clip. Preserve factual meaning, do not add unsupported claims, and fit within {duration} seconds. Output JSON with script, language, tone, and timing suggestions.
```

Mixing strategy：

- 可選原始人聲保留、降低、或替換。
- voiceover timing 必須對齊畫面段落。
- 避免 Phase 2 做，因為 TTS 口條、版權聲線、timing 會大幅增加 QA 複雜度。

## 16. Rendering 架構

第一版使用 FFmpeg，優先 NVENC，fallback `libx264`。

策略：

- 用 Python `subprocess.run([...])` 直接呼叫 FFmpeg list args，避免 Windows/Linux quoting 問題。
- `ffmpeg-python` 可讀性好，但複雜 filter graph、動態 ASS path escaping、NVENC fallback 時直接 subprocess 更可控。
- command builder 負責產生命令，不直接執行。
- render backend 負責執行、捕捉 stderr、產出 `render_report.json`。
- encoder fallback：依 config 嘗試 `h264_nvenc` -> `hevc_nvenc` -> `libx264`；失敗寫入 report 並換下一個。
- 長影片避免重跑：每個 clip 獨立 render plan/report；成功 clip 不重跑。

注意：

- Windows ASS subtitle path 需要 escape colon/backslash，建議先複製到 job 相對路徑並轉 POSIX-like filter path。
- NVENC preset/bitrate 要 config-driven，不要寫死。
- 9:16 crop 建議由 crop_track 生成 filter expressions 或先簡化為 static/segment crop，Phase 2 可先做 segment-level crop。

## 17. NVIDIA-ready 架構規劃

### 本地 Python MVP 未來瓶頸

- ASR 長音訊推理。
- LLM chunk analysis latency。
- Vision face/object detection。
- FFmpeg render I/O 與 encode。
- 單機 filesystem artifact contention。
- 單 process orchestration。

### 適合 GPU worker 的模組

- ASR worker。
- Vision worker。
- Render worker with NVENC。
- LLM worker 或 model server client。
- TTS worker future。

### 適合 Triton / TensorRT 的模組

- Vision detection model，如 YOLO face/person。
- segmentation/classification model。
- 部分 ASR/embedding model 視 backend 而定。
- 不要一開始把 faster-whisper 強行塞進 Triton，先讓 ASR backend 可替換。

### 保持 FFmpeg / NVENC 的模組

- decode/encode/transcode/render packaging。
- subtitles burn-in。
- audio extraction/mixing 初版。

### 是否需要 queue/database/storage/model registry

Phase 2 不需要。Phase 4 需要：

- message queue：Redis/RQ、Celery、NATS 或 RabbitMQ。
- job database：PostgreSQL 或 SQLite for local。
- object storage：S3-compatible/MinIO，用於影片與 artifacts。
- model registry：至少用 config + model path/version；進階用 MLflow/HF Hub/internal registry。

### Config profiles

需要：

- `local_windows.yaml`
- `local_linux.yaml`
- `nvidia_workstation.yaml`
- `nvidia_server.yaml`
- `jetson_edge.yaml`

### Dockerfile 與 GPU runtime

Docker strategy：

- base image：NVIDIA CUDA runtime/devel image。
- 安裝 FFmpeg with NVENC 支援。
- Python dependencies 分層。
- model cache volume。
- job artifact volume。
- expose API server only in Phase 4。

GPU runtime：

- 使用 NVIDIA Container Toolkit。
- `--gpus all` 或 compose `deploy.resources.reservations.devices`。
- 不在程式內假設 GPU index 0；由 config/env 指定。

### 避免 vendor lock-in

用 backend interface 抽象模型與 render，但在 NVIDIA profile 中啟用 CUDA/NVENC/TensorRT/Triton。也就是「介面不綁 NVIDIA，profile 善用 NVIDIA」。

## 18. CLI 設計

```bash
python main.py analyze --input input.mp4 --job-id demo001
python main.py transcribe --job-id demo001
python main.py select-clips --job-id demo001
python main.py crop --job-id demo001
python main.py subtitle --job-id demo001
python main.py render --job-id demo001
python main.py run-all --input input.mp4 --job-id demo001
```

通用參數：

- `--config`：指定 YAML。
- `--device`：`auto | cpu | cuda | cuda:0`。
- `--compute-type`：`float16 | int8_float16 | int8`。
- `--model-size`：ASR model。
- `--llm-backend`：`ollama | vllm | llama_cpp | openai_compatible`。
- `--llm-model`：LLM model name。
- `--target-format`：`9:16 | 1:1 | 16:9`。
- `--num-clips`：輸出 clip 數。
- `--min-duration` / `--max-duration`：clip 長度。
- `--resume`：跳過已完成 artifact。
- `--debug`：輸出更多中間資訊。

Command 作用：

- `analyze`：建立 job、跑 ffprobe、寫 `video_info.json`。
- `transcribe`：抽音訊、ASR、寫 transcript/words。
- `select-clips`：segment transcript、LLM 分析、validation。
- `crop`：分析 clip 視覺主體並產生 crop track。
- `subtitle`：根據 words 與 clip 產生 ASS。
- `render`：根據 render plan 輸出 MP4。
- `run-all`：依序跑完整 MVP pipeline。

## 19. Docker / NVIDIA migration 初步建議

短期不要先寫複雜 compose stack。先保留：

- `docker/Dockerfile`：單 worker image。
- `docker/docker-compose.yml`：Phase 4 才加 API/queue/worker。
- `docker/nvidia-runtime-notes.md`：記錄 NVIDIA Container Toolkit、driver/CUDA compatibility、NVENC 驗證命令。

先確保程式：

- 不使用 Windows-only path。
- 所有 subprocess args 用 list。
- 所有 artifact path 相對 job dir。
- 所有 GPU device 由 config/env 控制。
- model path/cache path 可掛 volume。

## 20. Performance profiling plan

每個 stage 記錄：

- input duration、frame size、fps。
- elapsed seconds。
- throughput：ASR audio seconds/sec、vision sampled frames/sec、render fps。
- GPU memory peak，可用 `nvidia-smi` sampling。
- CPU/RAM/disk free。
- cache hit/miss。
- encoder used。
- failure/fallback reason。

Phase 2 benchmark matrix：

- 1 分鐘、5 分鐘、30 分鐘素材。
- 720p、1080p、4K input。
- Level B/C/D profile。
- `medium` vs `distil-large-v3`。
- NVENC vs libx264。
- detection fps 1/3/5。

## 21. 第二階段實作 checklist

1. 建立 `pyproject.toml`、package layout、basic tests。
2. 實作 config loader + CLI overrides。
3. 實作 job manager + artifact read/write helpers。
4. 實作 `analyze`：FFprobe -> `video_info.json`。
5. 實作 `extract_audio`：FFmpeg -> `audio.wav`。
6. 實作 faster-whisper backend -> `transcript.json` / `words.json`。
7. 實作 transcript chunker -> `segments.json`。
8. 實作 Ollama LLM backend + prompt/schema validation。
9. 實作 clip validation：duration、overlap、sentence boundary。
10. 實作 MediaPipe/OpenCV low-fps face detection。
11. 實作 crop planner：center fallback、EMA smoothing、boundary clamp。
12. 實作 ASS writer：中文/英文 basic caption chunking。
13. 實作 render command builder + NVENC fallback。
14. 實作 debug/render/quality report。
15. 加入 `run-all --resume`。
16. 用 1-3 分鐘素材跑 smoke test。
17. 用 30 分鐘素材跑 chunk/resume/profiling test。

