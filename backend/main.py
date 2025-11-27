import uvicorn
import uuid
import os
import subprocess 
import json 

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse 
from pydantic import BaseModel

# --- Configuration ---
app = FastAPI()
STORAGE_DIR = "rendered_videos"
os.makedirs(STORAGE_DIR, exist_ok=True) # Ensure the storage directory exists

# Simple in-memory dictionary for job tracking
job_status = {}

class OverlayMetadata(BaseModel):
    # Data structure definition
    overlays: list

# --- FFMPEG Rendering Function (The Core Background Task) ---
def render_video_task(job_id: str, video_path: str, metadata_str: str):
    """Dynamically builds and executes the ffmpeg command based on overlay metadata."""
    
    try:
        job_status[job_id]["status"] = "PROCESSING"
        print(f"[{job_id}] Processing started. Input: {video_path}")
        
        # Parse the JSON string metadata
        metadata = json.loads(metadata_str)
        overlays = metadata.get('overlays', [])

        output_path = os.path.join(STORAGE_DIR, f"{job_id}_final.mp4")
        
        # --- 1. Prepare Command Components ---
        
        input_files = ["ffmpeg", "-y", "-i", video_path]
        filter_complex_list = []
        current_stream = "[0:v]" 
        filter_count = 0 
        asset_input_index = 1 # Tracks the index of additional inputs (1:v, 2:v, etc.)
        
        # --- 2. Process Overlays and Build Filtergraph ---
        for overlay in overlays:
            filter_count += 1
            start = overlay['start_time']
            end = overlay['end_time']
            new_stream = f"[v{filter_count}]"

            # --- TEXT OVERLAY ---
            if overlay['type'] == 'text':
                text_content = overlay['content'].replace("'", "\\'") # Escape quotes
                
                # Use the x_pos and y_pos coordinates directly from the frontend
                x_pos = overlay.get('x_pos', 50) 
                y_pos = overlay.get('y_pos', 50) 
                
                # FFMPEG Text Filter Command: Positioning (x, y) and Timing (enable)
                text_filter = (
                    f"drawtext=text='{text_content}':"
                    f"fontfile='{os.path.join(os.getcwd(), 'assets', 'Arial.ttf')}':" 
                    f"fontcolor=yellow:fontsize=48:x={x_pos}:y={y_pos}:"
                    f"enable='between(t,{start},{end})'"
                )
                
                filter_complex_list.append(f"{current_stream}{text_filter}{new_stream}")
                current_stream = new_stream 

            # --- IMAGE OVERLAY (Example for logo.png in assets/) ---
            elif overlay['type'] == 'image':
                image_path = os.path.join(os.getcwd(), 'assets', 'logo.png') 
                
                if not os.path.exists(image_path):
                    print(f"[{job_id}] ERROR: Image asset not found at {image_path}")
                    continue 

                input_files.extend(["-i", image_path])
                
                scale_stream = f"[img_scaled{filter_count}]"
                scale_filter = f"[{asset_input_index}:v] scale=150:-1 {scale_stream}"
                
                # Overlay chain: [video][image_scaled] overlay=...
                overlay_filter = (
                    f"overlay=x=W-w-20:y=20:" 
                    f"enable='between(t,{start},{end})'"
                )
                
                filter_complex_list.append(f"{scale_filter}; {current_stream}{scale_stream}{overlay_filter}{new_stream}")
                current_stream = new_stream 
                asset_input_index += 1 
        
        # --- 3. Construct the Final Command ---
        
        filter_complex_string = ", ".join(filter_complex_list)
        
        command = input_files + [
            "-filter_complex", 
            filter_complex_string, 
            "-map", current_stream, 
            "-map", "0:a?",         
            "-c:v", "libx264",      
            "-crf", "23",           
            "-preset", "veryfast",  
            "-c:a", "aac",          
            "-b:a", "192k",
            output_path
        ]

        print(f"[{job_id}] FFMPEG Command: {' '.join(command)}")
        
        # --- 4. Execute the Command ---
        result = subprocess.run(command, check=True, capture_output=True, text=True) 

        job_status[job_id]["result_path"] = output_path
        job_status[job_id]["status"] = "COMPLETE"
        print(f"[{job_id}] Rendering COMPLETE. Result: {output_path}")

    except subprocess.CalledProcessError as e:
        print(f"[{job_id}] FFMPEG EXECUTION FAILED. Stderr: {e.stderr}")
        job_status[job_id]["status"] = "FAILED"
        
    except Exception as e:
        print(f"[{job_id}] Error rendering job: {e}")
        job_status[job_id]["status"] = "FAILED"
        
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)
            print(f"[{job_id}] Cleaned up temp file.")

# --- Endpoints ---

@app.post("/upload")
async def upload_video(
    metadata: str,                         
    background_tasks: BackgroundTasks,     
    video_file: UploadFile = File(...),    
):
    """Accepts video file and overlay metadata, starts background rendering job."""
    
    job_id = str(uuid.uuid4())
    temp_video_path = os.path.join(STORAGE_DIR, f"{job_id}_{video_file.filename}")

    # 1. Save the uploaded video file
    try:
        content = await video_file.read()
        with open(temp_video_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save file: {e}")

    # 2. Store initial job status and data
    job_status[job_id] = {
        "status": "PENDING",
        "video_path": temp_video_path,
        "metadata": metadata, 
        "result_path": None
    }

    # 3. Start the background rendering task
    background_tasks.add_task(render_video_task, job_id, temp_video_path, metadata)

    return {"job_id": job_id, "status": "PENDING"}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    """Returns the processing status of a job."""
    
    if job_id not in job_status:
        raise HTTPException(status_code=404, detail="Job not found")
        
    return {"job_id": job_id, "status": job_status[job_id]["status"]}


@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """Returns the downloadable rendered video file if COMPLETE."""
    
    if job_id not in job_status:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job_status[job_id]["status"]
    
    if status != "COMPLETE":
        raise HTTPException(status_code=409, detail=f"Video processing not ready. Status: {status}")
        
    result_path = job_status[job_id]["result_path"]
    
    if not result_path or not os.path.exists(result_path):
        raise HTTPException(status_code=500, detail="Rendered file missing.")

    # Serve the file for download
    return FileResponse(
        path=result_path,
        media_type='video/mp4',
        filename=os.path.basename(result_path)
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)