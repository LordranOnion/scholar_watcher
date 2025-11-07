# scholar_watcher

Daemon for updating an RSS feed about new papers that are published with the keywords of our interest.

## Installing

1. Clone the repo into /opt
2. Download and install the requirements.txt file with "pip3 install -r requirements.txt" inside a venv.
3. Launch the app with "uvicorn scholar_discord_watcher:app --host 0.0.0.0 --port 8080"

You should see something like "INFO:     Uvicorn running on http://0.0.0.0:8080".

You will find the application running on "http://localhost:8080/".

# ENJOY! 😎
