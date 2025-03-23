# File: ota_github_server.py

import base64
import os
import zipfile
import io
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
from functools import partial
import json
import requests # type: ignore

CREDS_FILE = ".ota_credentials" if os.path.exists(".ota_credentials") \
    else os.path.expanduser("~/.ota_credentials")
PROJECTS_FILE = ".ota_projects.json" if os.path.exists(".ota_projects.json") \
      else os.path.expanduser("~/.ota_projects.json")

USERNAME = PASSWORD = ""
if os.path.exists(CREDS_FILE):
    with open(CREDS_FILE, encoding="utf-8") as f:  # Specify encoding as utf-8
        line = f.readline().strip()
        if ";" in line:
            USERNAME, PASSWORD = line.split(";", 1)
AUTH_KEY = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

OTA_DIR = os.path.abspath("ota_files")
SYNC_INTERVAL = 3600

PROJECTS = {}
if os.path.exists(PROJECTS_FILE):
    with open(PROJECTS_FILE, encoding="utf-8") as pf:
        PROJECTS = json.load(pf)

class AuthHandler(SimpleHTTPRequestHandler):
    """
    Custom request handler for handling authentication and serving OTA updates.

    Inherits from SimpleHTTPRequestHandler.
    """

    def do_auth_head(self):
        """
        Send HTTP 401 response with the 'WWW-Authenticate' header.
        """
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="OTA Server"')
        self.end_headers()

    def is_authenticated(self):
        """
        Check if the request is authenticated.

        Returns:
            bool: True if the request is authenticated, False otherwise.
        """
        auth_header = self.headers.get('Authorization')
        return auth_header == f"Basic {AUTH_KEY}"

    def list_directory(self, path):
        """
        Override the default behavior of listing the directory contents.

        Args:
            path (str): The path of the directory.

        Returns:
            None
        """
        project = os.path.basename(path.rstrip('/'))
        if project in PROJECTS:
            versions = sorted([d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))])
            version_file_path = os.path.join(path, "version")
            current_version = None
            if os.path.exists(version_file_path):
                with open(version_file_path, "r", encoding="utf-8") as vf:
                    current_version = vf.read().strip()

            html = f"<html><head><title>{project} - OTA</title></head><body>"
            html += f"<h1>{project} - Synced Versions</h1>"
            if current_version:
                html += f"<p><strong>Current Version:</strong> {current_version}</p>"
            html += f"<p><a href='/sync_now?project={project}'>Sync Now</a> | <a href='/rollback?project={project}'>Rollback</a></p>"
            html += "<ul>"
            for ver in versions:
                html += f'<li><a href="{ver}/">{ver}</a>'
                py_files = [f for f in os.listdir(os.path.join(path, ver)) if f.endswith(".py")]
                if py_files:
                    html += " <ul>"
                    for f in py_files:
                        html += f'<li><a href="{ver}/{f}">{f}</a></li>'
                    html += "</ul>"
                html += "</li>"
            html += "</ul></body></html>"
            encoded = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return None

        return super().list_directory(path)

    def do_GET(self):
        """
        Handle GET requests.

        Returns:
            None
        """
        if not self.is_authenticated():
            self.do_auth_head()
            self.wfile.write(b"Authentication required.")
            return

        parsed_path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_path.query)

        if parsed_path.path == "/set_version":
            new_version = query.get("version", [None])[0]
            project = query.get("project", [None])[0]
            if new_version and project in PROJECTS:
                version_file = os.path.join(OTA_DIR, project, "version")
                with open(version_file, "w", encoding="utf-8") as vf:
                    vf.write(new_version)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"Version for {project} set to {new_version}".encode())
                return
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing or invalid parameters.")
                return

        if parsed_path.path == "/sync_now":
            project = query.get("project", [None])[0]
            if project in PROJECTS:
                try:
                    repo = PROJECTS[project]
                    tags = get_latest_tags(repo, count=2)
                    for tag in tags:
                        fetch_github_release(tag, project, repo)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(f"Manually synced {project} tags: {', '.join(tags)}".encode())
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(e).encode())
                return

        if parsed_path.path == "/rollback":
            project = query.get("project", [None])[0]
            if project in PROJECTS:
                project_path = os.path.join(OTA_DIR, project)
                versions = sorted([d for d in os.listdir(project_path) if os.path.isdir(os.path.join(project_path, d))])
                version_file = os.path.join(project_path, "version")
                if len(versions) >= 2:
                    with open(version_file, "w", encoding="utf-8") as vf:
                        vf.write(versions[-2])
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(f"Rolled back {project} to {versions[-2]}".encode())
                else:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"No previous version to roll back to.")
                return

        return super().do_GET()

def get_latest_tags(repo, count=2):
    """
    Retrieves the latest tags from a GitHub repository.

    Args:
        repo (str): The name of the repository in the format "owner/repo".
        count (int, optional): The number of latest tags to retrieve. Defaults to 2.

    Returns:
        list: A list of the names of the latest tags, up to the specified count.
    """
    url = f"https://api.github.com/repos/{repo}/tags"
    r = requests.get(url)
    r.raise_for_status()
    tags = r.json()
    return [tag["name"] for tag in tags[:count]] if tags else []

def fetch_github_release(tag: str, project: str, repo: str):
    """
    Fetches a specific release from a GitHub repository and saves it to the local file system.

    Args:
        tag (str): The tag of the release to fetch.
        project (str): The name of the project.
        repo (str): The name of the GitHub repository.

    Returns:
        None
    """
    release_path = os.path.join(OTA_DIR, project, tag)
    if os.path.exists(release_path) and os.listdir(release_path):
        print(f"[{project}] Tag {tag} already synced.")
        return

    os.makedirs(release_path, exist_ok=True)
    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
    print(f"[{project}] Fetching zip: {zip_url}")
    zip_res = requests.get(zip_url)
    zip_res.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
        for zip_info in z.infolist():
            if zip_info.filename.endswith(".py") and "/src/" in zip_info.filename:
                target_name = os.path.basename(zip_info.filename)
                with open(os.path.join(release_path, target_name), 'wb') as f:
                    f.write(z.read(zip_info))

    version_file = os.path.join(OTA_DIR, project, "version")
    with open(version_file, "w", encoding="utf-8") as vf:
        vf.write(tag)

    print(f"[{project}] Fetched release {tag} to {release_path} and updated version file.")

def sync_latest_releases():
    """
    Synchronizes the latest releases for each project in the PROJECTS dictionary.

    This function iterates over each project in the PROJECTS dictionary and retrieves the latest
    tags from the corresponding GitHub repository. It then fetches the GitHub release for each
    tag and prints a message indicating the synchronization status.

    Raises:
        Exception: If an error occurs during the synchronization process.

    """
    for project, repo in PROJECTS.items():
        try:
            tags = get_latest_tags(repo, count=2)
            for tag in tags:
                print(f"[{project}] Syncing tag: {tag}")
                fetch_github_release(tag, project, repo)
        except requests.exceptions.RequestException as e:
            print(f"[{project}] Sync error: {e}")

def periodic_sync():
    """
    Periodically syncs the latest releases.

    This function runs in an infinite loop and periodically calls the `sync_latest_releases` function
    to synchronize the latest releases. It sleeps for the specified `SYNC_INTERVAL` between each sync.

    """
    while True:
        sync_latest_releases()
        time.sleep(SYNC_INTERVAL)

if __name__ == '__main__':
    threading.Thread(target=periodic_sync, daemon=True).start()
    handler = partial(AuthHandler, directory=OTA_DIR)
    server = HTTPServer(('0.0.0.0', 8000), handler)
    print("OTA Server running at http://0.0.0.0:8000")
    server.serve_forever()
