import subprocess
import sys
import os
import signal
import time

def main():
    print("Starting application launcher with Hypercorn...")
    
    # Start main.py
    print("Starting main.py...")
    main_process = subprocess.Popen([sys.executable, "main.py"])
    
    # Give main.py a moment to start
    time.sleep(2)
    
    # Start hypercorn instead of uvicorn
    print("Starting hypercorn server...")
    port = os.environ.get("PORT", "8000")
    print(f"Using port: {port}")
    
    hypercorn_process = subprocess.Popen([
        sys.executable, "-m", "hypercorn", 
        "app.agent_api:app", 
        "--bind", f"0.0.0.0:{port}"
    ])
    
    # Function to handle termination
    def signal_handler(sig, frame):
        print("Received termination signal, shutting down...")
        main_process.terminate()
        hypercorn_process.terminate()
        main_process.wait()
        hypercorn_process.wait()
        sys.exit(0)
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Wait for either process to exit
    try:
        while True:
            # Check if processes are still alive
            if main_process.poll() is not None:
                print("main.py process exited")
                break
            if hypercorn_process.poll() is not None:
                print("hypercorn process exited")
                break
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("Keyboard interrupt received")
    except Exception as e:
        print(f"Error occurred: {e}")
    finally:
        print("Cleaning up processes...")
        # Terminate both processes
        main_process.terminate()
        hypercorn_process.terminate()
        
        # Wait for them to finish
        main_process.wait()
        hypercorn_process.wait()
        print("All processes terminated")

if __name__ == "__main__":
    main()
